"""
Direct unit tests for TreeWorker._on_remote_node_close rcopy paths
(lines 465-468, 478-480, 482, 487).

When the worker is doing rcopy (reverse copy: source AND reverse=True),
_on_remote_node_close finalizes by extracting the tar that the remote
streamed back. Edge cases covered here:

  - 465-468: trailing buffer non-empty (`if len(buf) > 0:` True)
  - 478-480: tarfile.extractall raises IOError -> error message
             dispatched as stderr via msgline
  - 482:     tarfile.open itself fails (TarError) -> tmptar stays None
             so the finally block skips close()
  - 487:     no buffer ever received from this node (else branch)
"""

import tarfile
import tempfile
from unittest.mock import MagicMock

import pytest


def _make_valid_empty_tar_tempfile():
    """Return a TemporaryFile containing a valid (empty) tar archive,
    seeked to end so further .write() appends."""
    tf = tempfile.TemporaryFile()
    tar = tarfile.open(fileobj=tf, mode='w:')
    tar.close()  # writes the two empty end-of-archive blocks
    return tf


def _setup_rcopy_worker(make_worker, tmp_path):
    """Build a worker configured for rcopy mode with one in-flight node."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()

    w = make_worker(
        nodes='node1',
        command=None,
        source=str(src),
        dest=str(dest),
        reverse=True,
    )
    w.gwtargets['gw1'] = {'node1'}
    w._target_count = 1
    return w


def test_remote_close_rcopy_no_buffer_received_logs_else(make_worker, tmp_path):
    """Hits line 487: node closes but no rcopy data was ever received
    for it. The `else` branch logs a debug and the function continues."""
    w = _setup_rcopy_worker(make_worker, tmp_path)
    # _rcopy_bufs is empty for node1 -> hits the else branch
    assert 'node1' not in w._rcopy_bufs

    w._on_remote_node_close('node1', 0, 'gw1')

    # node was removed from gwtargets and the gateway entry was released
    assert 'gw1' not in w.gwtargets
    # _close_count incremented
    assert w._close_count == 1


def test_remote_close_rcopy_partial_buffer_is_flushed(make_worker, tmp_path):
    """Hits lines 465-468: when _rcopy_bufs has trailing bytes left
    over at close time, they are written to the tarfile object before
    extraction."""
    w = _setup_rcopy_worker(make_worker, tmp_path)

    tarfile_obj = _make_valid_empty_tar_tempfile()
    w._rcopy_tars['node1'] = tarfile_obj
    w._rcopy_bufs['node1'] = b'XYZ'  # non-empty trailing buf

    # Should not raise: empty tar is parseable and extractall is a no-op
    w._on_remote_node_close('node1', 0, 'gw1')

    # rcopy state for the node was cleaned up after extraction
    assert 'node1' not in w._rcopy_bufs
    assert 'node1' not in w._rcopy_tars
    # gateway clean-up still happens
    assert 'gw1' not in w.gwtargets


def test_remote_close_rcopy_ioerror_in_extract_routed_to_stderr(make_worker, tmp_path,
                                                                 recording_handler,
                                                                 monkeypatch):
    """Hits lines 478-480: tmptar.extractall raises IOError -> the
    error message is routed back as a stderr msgline (so the handler
    sees it as 'stderr' output from the failing node) rather than
    crashing the worker."""
    # We need a handler to actually OBSERVE that the stderr msg fired.
    w = make_worker(
        nodes='node1',
        handler=recording_handler,
        command=None,
        source=str(tmp_path / "missing-src"),  # path used only as label
        dest=str(tmp_path / "dest"),
        reverse=True,
    )
    w.gwtargets['gw1'] = {'node1'}
    w._target_count = 1

    tarfile_obj = _make_valid_empty_tar_tempfile()
    w._rcopy_tars['node1'] = tarfile_obj
    w._rcopy_bufs['node1'] = b''  # empty -> skips lines 474-476

    # Monkeypatch tarfile.open to return a Mock whose .extractall raises
    # IOError. We must preserve the rest of the API surface (getmembers,
    # close) so the finally block doesn't blow up.
    fake_tar = MagicMock(name='fake-tar')
    fake_tar.getmembers.return_value = []
    fake_tar.extractall.side_effect = IOError('simulated extract failure')

    def open_capturing(fileobj=None, **kwargs):
        # Return our fake tar regardless of args.
        return fake_tar

    monkeypatch.setattr(tarfile, 'open', open_capturing)

    # Must not raise: IOError is caught and re-dispatched as stderr.
    w._on_remote_node_close('node1', 0, 'gw1')

    # fake_tar was closed in the finally block
    fake_tar.close.assert_called_once()

    # The IOError was reported via _on_remote_node_msgline as 'stderr'.
    # _on_remote_node_msgline normally dispatches to the handler's
    # ev_read with sname='stderr' (via DistantWorker._on_node_msgline).
    # Look for a stderr entry in recording_handler's ev_read calls.
    stderr_reads = [c for c in recording_handler.ev_read_calls if c[1] == 'stderr']
    assert len(stderr_reads) == 1
    # the message body is the stringified error encoded as bytes (#663)
    assert stderr_reads[0][2] == b'simulated extract failure'


def test_remote_close_rcopy_open_error_routed_to_stderr(make_worker, tmp_path,
                                                        recording_handler):
    """Hits line 482 False branch: tarfile.open fails on a corrupt tar
    payload (TarError), the error is routed as a stderr msgline, and
    tmptar stays None so the finally block skips close()."""
    w = make_worker(
        nodes='node1',
        handler=recording_handler,
        command=None,
        source=str(tmp_path / "missing-src"),  # path used only as label
        dest=str(tmp_path / "dest"),
        reverse=True,
    )
    w.gwtargets['gw1'] = {'node1'}
    w._target_count = 1

    # garbage payload: tarfile.open raises ReadError (a TarError)
    tarfile_obj = tempfile.TemporaryFile()
    tarfile_obj.write(b'this is not a tar archive')
    w._rcopy_tars['node1'] = tarfile_obj
    w._rcopy_bufs['node1'] = b''

    # Must not raise: TarError is caught and re-dispatched as stderr.
    w._on_remote_node_close('node1', 0, 'gw1')

    stderr_reads = [c for c in recording_handler.ev_read_calls if c[1] == 'stderr']
    assert len(stderr_reads) == 1
    assert isinstance(stderr_reads[0][2], bytes)

    # rcopy state for the node was cleaned up despite the open failure
    assert 'node1' not in w._rcopy_bufs
    assert 'node1' not in w._rcopy_tars
    assert 'gw1' not in w.gwtargets

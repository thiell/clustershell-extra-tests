"""
PEP 706: TreeWorker.rcopy must forward filter='fully_trusted' to
tarfile.extractall when the runtime supports it, and must omit the kwarg
otherwise (so Python 2.7 and pre-backport 3.x patch releases don't crash
with TypeError).

'fully_trusted' is the deliberate choice over 'tar'/'data' because tarballs
are produced by trusted ClusterShell gateways and the secure filters would
strip S_IWGRP|S_IWOTH (downgrading e.g. 0664 to 0644).

Detection is via tarfile.data_filter (added in the same gh-104012 patchset
as the filter= kwarg).
"""

import io
import tarfile

import pytest

from ClusterShell.Worker import Tree as tree_mod

pytestmark = pytest.mark.skipif(
    not hasattr(tree_mod, "_TAR_EXTRACT_KWARGS"),
    reason="ClusterShell predates the PEP 706 fix (no Tree._TAR_EXTRACT_KWARGS)",
)


class _FakeTar:
    """Stand-in for tarfile.open(...). Records extractall() kwargs."""

    def __init__(self):
        self.extractall_calls = []
        self.closed = False

    def getmembers(self):
        return []

    def extractall(self, **kwargs):
        self.extractall_calls.append(kwargs)

    def close(self):
        self.closed = True


def _drive_rcopy_close(monkeypatch, make_worker, fake_task, kwargs_override):
    """Trigger _on_remote_node_close on a forged rcopy state.

    Returns the _FakeTar that captured the extractall() call.
    """
    worker = make_worker(
        nodes='node1',
        handler=None,
        timeout=None,
        command=None,
        source='/src',
        dest='/dst',
        reverse=True,
    )
    worker._started = True
    worker.gwtargets = {'gw1': {'node1'}}

    # Forge an in-progress rcopy state: empty buffer + a BytesIO acting as
    # the tar fileobj. tarfile.open is monkeypatched, so the BytesIO content
    # is never parsed; we only need flush()/seek() to be callable.
    worker._rcopy_bufs['node1'] = b''
    worker._rcopy_tars['node1'] = io.BytesIO()

    fake_tar = _FakeTar()
    monkeypatch.setattr(tree_mod.tarfile, 'open',
                        lambda **kw: fake_tar)
    monkeypatch.setattr(tree_mod, '_TAR_EXTRACT_KWARGS', kwargs_override)

    worker._on_remote_node_close('node1', 0, 'gw1')

    return fake_tar


def test_extractall_forwards_filter_data(monkeypatch, make_worker, fake_task):
    """On a runtime with tarfile.data_filter, extractall must get filter='fully_trusted'."""
    fake_tar = _drive_rcopy_close(monkeypatch, make_worker, fake_task,
                                  kwargs_override={'filter': 'fully_trusted'})

    assert len(fake_tar.extractall_calls) == 1
    assert fake_tar.extractall_calls[0].get('path') == '/dst'
    assert fake_tar.extractall_calls[0].get('filter') == 'fully_trusted'
    assert fake_tar.closed


def test_extractall_omits_kwarg_on_legacy_python(monkeypatch, make_worker,
                                                 fake_task):
    """Legacy Python (Py2.7, pre-backport 3.x): no filter= kwarg forwarded."""
    fake_tar = _drive_rcopy_close(monkeypatch, make_worker, fake_task,
                                  kwargs_override={})

    assert len(fake_tar.extractall_calls) == 1
    assert fake_tar.extractall_calls[0].get('path') == '/dst'
    assert 'filter' not in fake_tar.extractall_calls[0]
    assert fake_tar.closed


def test_module_constant_matches_runtime_capability():
    """_TAR_EXTRACT_KWARGS is wired to tarfile.data_filter availability."""
    if hasattr(tarfile, 'data_filter'):
        assert tree_mod._TAR_EXTRACT_KWARGS == {'filter': 'fully_trusted'}
    else:
        assert tree_mod._TAR_EXTRACT_KWARGS == {}

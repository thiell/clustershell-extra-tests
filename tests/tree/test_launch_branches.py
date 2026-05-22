"""
Direct unit tests for three uncovered branches inside TreeWorker._launch:

  - line 271         copy-arcname when source is a directory and dest
                     has no trailing slash
  - lines 311-318    `else` branch when self.remote is False (local
                     worker class instantiated and scheduled)
  - lines 348-349    `except OSError -> raise WorkerError(exc)`
                     when the tar pipeline fails

_launch has many collaborators. Our fixture pattern already covers them:
  - self.router.dispatch  -> FakeRouter.set_dispatch([...])
  - self.task.info(...)   -> FakeTask returns 64 for "fanout"
  - self.task.default(...) -> FakeTask.defaults[key] or fresh MagicMock
  - self.task.schedule(w) -> FakeTask.scheduled.append(w)
"""

import tarfile

import pytest
from unittest.mock import MagicMock

from ClusterShell.NodeSet import NodeSet
from ClusterShell.Worker.Worker import WorkerError


def test_launch_copy_dir_source_dest_no_trailing_slash(make_worker, tmp_path):
    """Hits line 271: arcname = basename(self.dest) when source is a
    directory and dest does NOT end with '/'.

    With an empty dispatch from FakeRouter, the for-next_hops loop is
    skipped, so we exit _launch cleanly after building the tar in
    memory. The tar.add path needs a real source dir, hence tmp_path.
    """
    src = tmp_path / "srcdir"
    src.mkdir()

    w = make_worker(
        nodes='node1',
        command=None,
        source=str(src),
        dest='/tmp/destname',  # no trailing slash -> hits line 271
    )

    w._launch(w.nodes)  # must not raise

    # next_hops was empty so no child worker was created
    assert w.workers == []
    # gwtargets stayed empty so _write_remote was a no-op
    assert w.gwtargets == {}


def test_launch_remote_false_uses_local_worker_class(make_worker, fake_router, fake_task):
    """Hits lines 311-318: the `else` block when self.remote is False.

    Verifies:
      - task.default('local_worker') is queried
      - the returned class is invoked with (nodes, command, handler,
        timeout, stderr) keyword args
      - the returned instance is scheduled and appended to self.workers
    """
    w = make_worker(nodes='node1', remote=False)

    # Make _distribute return one DIRECT-child pair (gw == targets).
    same = NodeSet('node1')
    fake_router.set_dispatch([(same, same)])

    # Configure task.default('local_worker') to return a class-like Mock
    # whose calls we can inspect.
    LocalWorkerClass = MagicMock(name='LocalWorker')
    fake_task.defaults['local_worker'] = LocalWorkerClass

    w._launch(w.nodes)

    # The class was called once with the expected kwargs
    LocalWorkerClass.assert_called_once()
    call_kwargs = LocalWorkerClass.call_args.kwargs
    assert call_kwargs['command'] == 'true'
    assert call_kwargs['handler'] is w.metahandler
    assert call_kwargs['timeout'] is None
    assert call_kwargs['stderr'] is False
    # NodeSet equality is by content
    assert call_kwargs['nodes'] == same

    # The returned instance was scheduled on the task
    assert len(fake_task.scheduled) == 1
    assert fake_task.scheduled[0] is LocalWorkerClass.return_value

    # And appended to the worker's children list
    assert w.workers == [LocalWorkerClass.return_value]
    # Internal counters advanced
    assert w._child_count == 1
    assert w._target_count == 1


def test_launch_tar_oserror_is_wrapped_as_workererror(make_worker, tmp_path, monkeypatch):
    """Hits lines 348-349: an OSError raised inside the tar pipeline
    must be wrapped in WorkerError so callers see a domain error
    rather than a stdlib one.

    Done by monkey-patching `tarfile.open` to raise OSError directly.
    This is safer than relying on a bogus source path, which would
    raise inside `tar.add` and produce a slightly different stacktrace.
    """
    src = tmp_path / "srcdir"
    src.mkdir()

    def boom(*args, **kwargs):
        raise OSError("simulated tarfile.open failure")

    monkeypatch.setattr(tarfile, 'open', boom)

    w = make_worker(
        nodes='node1',
        command=None,
        source=str(src),
        dest='/tmp/x',
    )

    with pytest.raises(WorkerError) as excinfo:
        w._launch(w.nodes)

    # The original OSError should be wrapped, not swallowed.
    assert 'simulated tarfile.open failure' in str(excinfo.value)

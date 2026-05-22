"""
Fixtures for direct-call unit testing of ClusterShell's TreeWorker.

Goal: build a TreeWorker instance that has all the attributes its
methods need, but without spinning up a real ClusterShell Engine.
That lets us call private methods (_emit_pickup, _check_ini,
_check_fini, _gateway_abort, abort, etc.) directly with controlled
inputs and assert on observable state.

Design choices:
  - Real ClusterShell.Event.EventHandler subclass for the handler
    (cheap and well-behaved).
  - Real NodeSet for node arguments (also well-behaved).
  - Hand-rolled FakeTask / FakeRouter exposing only the surface area
    Tree.py actually touches; MagicMock for the child workers/pchannels
    so we can inspect call signatures.
  - We overwrite worker.task / worker.router AFTER TreeWorker.__init__
    completes, so __init__ runs its normal code path without needing a
    real engine. This is the load-bearing trick that lets the rest of
    the tests stay short.
"""

from unittest.mock import MagicMock

import pytest

from ClusterShell.Event import EventHandler
from ClusterShell.NodeSet import NodeSet
from ClusterShell.Worker.Tree import TreeWorker


class RecordingHandler(EventHandler):
    """EventHandler that records every event call in plain lists.

    Plain lists make ordering and arg assertions trivial without
    poking at MagicMock internals.
    """

    def __init__(self):
        super().__init__()
        self.ev_start_calls = []
        self.ev_pickup_calls = []
        self.ev_read_calls = []
        self.ev_written_calls = []
        self.ev_hup_calls = []
        self.ev_timeout_calls = []
        self.ev_close_calls = []
        self.routing_calls = []

    def ev_start(self, worker):
        self.ev_start_calls.append(worker)

    def ev_pickup(self, worker, node):
        self.ev_pickup_calls.append((worker, str(node)))

    def ev_read(self, worker, node, sname, msg):
        self.ev_read_calls.append((str(node), sname, msg))

    def ev_written(self, worker, node, sname, size):
        self.ev_written_calls.append((str(node), sname, size))

    def ev_hup(self, worker, node, rc):
        self.ev_hup_calls.append((str(node), rc))

    def ev_timeout(self, worker):
        self.ev_timeout_calls.append(worker)

    def ev_close(self, worker, timedout):
        self.ev_close_calls.append(timedout)

    def _ev_routing(self, worker, arg):
        self.routing_calls.append(arg)


class HandlerWithoutTimeout(EventHandler):
    """Handler intentionally missing ev_timeout, so _check_fini's
    hasattr(handler, 'ev_timeout') guard can be exercised."""

    def __init__(self):
        super().__init__()
        self.ev_close_calls = []

    def ev_close(self, worker, timedout):
        self.ev_close_calls.append(timedout)
    # NOTE: no ev_timeout method on purpose


class AbortOnStartHandler(EventHandler):
    """Handler whose ev_start immediately aborts the worker.

    Used to exercise _check_ini's path where ev_start sets _aborted
    BEFORE the pending-pickup flush loop runs.
    """

    def __init__(self):
        super().__init__()
        self.ev_start_calls = []
        self.ev_pickup_calls = []

    def ev_start(self, worker):
        self.ev_start_calls.append(worker)
        worker.abort()

    def ev_pickup(self, worker, node):
        self.ev_pickup_calls.append(str(node))


class AbortOnNthPickupHandler(EventHandler):
    """Handler whose ev_pickup aborts the worker on the Nth call (1-based).

    Used to exercise _check_ini's inner `if self._aborted: break` —
    the very line we cleaned up in PR #615.
    """

    def __init__(self, abort_after_n):
        super().__init__()
        self.abort_after_n = abort_after_n
        self.ev_start_calls = []
        self.ev_pickup_calls = []

    def ev_start(self, worker):
        self.ev_start_calls.append(worker)

    def ev_pickup(self, worker, node):
        self.ev_pickup_calls.append(str(node))
        if len(self.ev_pickup_calls) >= self.abort_after_n:
            worker.abort()


class FakeRouter:
    """Stand-in for PropagationTreeRouter.

    .dispatch(nodeset) yields the pairs preconfigured via set_dispatch().
    Each pair is (gateway, NodeSet-of-targets). If gateway == targets,
    TreeWorker treats it as a direct child; otherwise gateway-routed.
    """

    def __init__(self):
        self.fanout = 64
        self._dispatch_result = []

    def set_dispatch(self, pairs):
        # pairs: list of (gw_nodeset, targets_nodeset) — both NodeSet
        self._dispatch_result = list(pairs)

    def dispatch(self, nodeset):
        return iter(self._dispatch_result)


class FakeTask:
    """Minimal stand-in for ClusterShell.Task.Task.

    Implements only the methods TreeWorker actually calls. Records
    each call so tests can assert against them.
    """

    def __init__(self, fanout=64):
        self._fanout = fanout
        self.topology = None  # tests can set this
        self._pchannels = {}
        self.scheduled = []
        self.shell_calls = []
        self.copy_calls = []
        self.released = []
        # Per-node timeouts recorded by DistantWorker._on_node_timeout
        # (which calls self.task._timeout_add(worker, key)).
        self.timeouts_added = []
        # default()-returned MagicMocks per key, so tests can pre-set
        # eg. task.defaults['local_worker'] = MyMock
        self.defaults = {}

    def info(self, key):
        if key == 'fanout':
            return self._fanout
        return None

    def default(self, key):
        if key in self.defaults:
            return self.defaults[key]
        return MagicMock(name='default(%s)' % key)

    def _default_router(self, router):
        return router  # passthrough

    def _pchannel(self, gateway, worker):
        key = str(gateway)
        if key not in self._pchannels:
            self._pchannels[key] = MagicMock(name='pchannel(%s)' % key)
        return self._pchannels[key]

    def _pchannel_release(self, gateway, worker):
        self.released.append(str(gateway))

    def shell(self, *args, **kwargs):
        w = MagicMock(name='shell-worker')
        self.shell_calls.append((args, kwargs))
        return w

    def copy(self, *args, **kwargs):
        w = MagicMock(name='copy-worker')
        self.copy_calls.append((args, kwargs))
        return w

    def schedule(self, worker):
        self.scheduled.append(worker)

    def _timeout_add(self, worker, key):
        # Called by DistantWorker._on_node_timeout to register a per-node timeout
        # on the task's internal set. Recording-only here.
        self.timeouts_added.append((worker, str(key)))

    def _rc_set(self, worker, key, rc):
        # Called by Worker._on_close when rc is not None. Recording-only here.
        if not hasattr(self, 'rcs'):
            self.rcs = []
        self.rcs.append((worker, str(key), rc))

    def _msg_add(self, worker, key, sname, msg):
        # Called by DistantWorker._on_node_msgline. Recording-only here.
        if not hasattr(self, 'msgs'):
            self.msgs = []
        self.msgs.append((worker, str(key), sname, msg))


# ---------- Pytest fixtures ----------


@pytest.fixture
def recording_handler():
    return RecordingHandler()


@pytest.fixture
def handler_without_timeout():
    return HandlerWithoutTimeout()


@pytest.fixture
def abort_on_start_handler():
    return AbortOnStartHandler()


@pytest.fixture
def make_abort_on_nth_pickup_handler():
    """Factory: make_abort_on_nth_pickup_handler(n) -> AbortOnNthPickupHandler."""
    return lambda n: AbortOnNthPickupHandler(abort_after_n=n)


@pytest.fixture
def fake_router():
    return FakeRouter()


@pytest.fixture
def fake_task():
    return FakeTask()


@pytest.fixture
def make_worker(fake_task, fake_router):
    """Factory: make_worker(nodes='node[1-3]', handler=None, timeout=None, **kwargs).

    Returns a TreeWorker with:
      * task replaced by FakeTask
      * router replaced by FakeRouter
      * NOT started — caller must invoke private methods directly
    """

    def factory(nodes='node[1-3]', handler=None, timeout=None, **kwargs):
        kwargs.setdefault('command', 'true')
        worker = TreeWorker(NodeSet(nodes), handler, timeout, **kwargs)
        # Overwrite engine-dependent attributes AFTER __init__ ran.
        worker.task = fake_task
        worker.router = fake_router
        return worker

    return factory

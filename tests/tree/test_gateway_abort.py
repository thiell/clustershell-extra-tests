"""
Direct unit tests for TreeWorker._gateway_abort and abort().

_gateway_abort(gateway):
    if gateway not in self.gwtargets:
        log warning; return
    targets = self.gwtargets[gateway]
    for target in NodeSet.fromlist(targets):
        self._on_remote_node_close(target, EX_PROTOCOL, gateway)

abort():
    self._aborted = True
    self._pending_pickups = []
    for worker in self.workers:
        worker.abort()
    for gateway in self.gwtargets.copy():
        self._gateway_abort(gateway)
"""

import logging
import os
from unittest.mock import MagicMock


def test_gateway_abort_unknown_gateway_warns_and_returns(make_worker, caplog):
    """Unknown gateway must log a warning and return without raising."""
    w = make_worker(handler=None)
    assert 'no-such-gw' not in w.gwtargets

    with caplog.at_level(logging.WARNING, logger='ClusterShell.Worker.Tree'):
        w._gateway_abort('no-such-gw')

    # at least one WARNING message mentioning the bogus gateway name
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any('no-such-gw' in m for m in msgs), \
        "expected a warning about 'no-such-gw'; got %r" % msgs


def test_gateway_abort_known_gateway_closes_each_target(make_worker):
    """Each target on the failed gateway gets _on_remote_node_close
    called with EX_PROTOCOL. We stub _on_remote_node_close to avoid
    exercising the full close path (which involves DistantWorker._on_node_close
    and writes to task internals we haven't faked)."""
    w = make_worker(handler=None)
    w.gwtargets['gw1'] = {'node1', 'node2', 'node3'}

    calls = []
    w._on_remote_node_close = lambda node, rc, gw: calls.append((str(node), rc, gw))

    w._gateway_abort('gw1')

    # NodeSet.fromlist orders nodes; sort our list for a stable assert.
    assert sorted(calls) == [
        ('node1', os.EX_PROTOCOL, 'gw1'),
        ('node2', os.EX_PROTOCOL, 'gw1'),
        ('node3', os.EX_PROTOCOL, 'gw1'),
    ]


def test_abort_aborts_each_child_worker_and_dispatches_to_gateways(make_worker):
    """abort() must:
       - set _aborted = True
       - clear _pending_pickups
       - call .abort() on each direct child worker
       - call _gateway_abort once per gateway in gwtargets
    """
    w = make_worker(handler=None)
    child1 = MagicMock(name='child1')
    child2 = MagicMock(name='child2')
    w.workers = [child1, child2]
    w.gwtargets = {'gw1': {'n1'}, 'gw2': {'n2', 'n3'}}
    w._pending_pickups = ['leftover1', 'leftover2']

    # stub _gateway_abort so we only assert on the dispatch, not the close path
    called_gws = []
    w._gateway_abort = lambda gw: called_gws.append(gw)

    w.abort()

    assert w._aborted is True
    assert w._pending_pickups == []
    child1.abort.assert_called_once()
    child2.abort.assert_called_once()
    assert sorted(called_gws) == ['gw1', 'gw2']


def test_abort_safe_with_no_children_and_no_gateways(make_worker):
    """The 'empty' baseline: abort() on a freshly-constructed worker
    must still set the flags without iterating anything."""
    w = make_worker(handler=None)
    assert w.workers == []
    assert w.gwtargets == {}

    w.abort()

    assert w._aborted is True
    assert w._pending_pickups == []

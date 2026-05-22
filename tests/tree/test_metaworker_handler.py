"""
Direct unit tests for event dispatch through MetaWorkerEventHandler and
the TreeWorker methods it calls into.

The cohesive grouping here covers four small uncovered regions:

  - line 78  MetaWorkerEventHandler.ev_written, `if metaworker.eh:` False side
  - 100-101  MetaWorkerEventHandler.ev_close timedout iteration
  - 512-515  TreeWorker._on_node_timeout (the non-gateway path)
  - 518-519  TreeWorker._on_routing_event (delegate to eh._ev_routing)

The ev_close-timedout test exercises 100-101 and 512-515 in one shot:
the meta-handler iterates the child's timed-out keys and dispatches
each into TreeWorker._on_node_timeout.
"""

from unittest.mock import MagicMock


def test_metaworker_ev_written_silent_when_eh_none(make_worker):
    """Hits the False side of line 78: `if metaworker.eh:`.

    A meta worker may legitimately have no handler — in that case
    ev_written should still set current_node / current_sname as a
    side effect but NOT try to dispatch.
    """
    w = make_worker(handler=None)
    fake_child = MagicMock(name='child')

    # Must not raise.
    w.metahandler.ev_written(fake_child, 'node1', 'stdout', 42)

    # The state side-effects still happen (they're above the guard).
    assert w.current_node == 'node1'
    assert w.current_sname == 'stdout'


def test_metaworker_ev_written_dispatches_when_eh_present(make_worker, recording_handler):
    """Hits the True side of line 78 — also closes any remaining
    branch arrow on the line and documents the positive path."""
    w = make_worker(handler=recording_handler)
    fake_child = MagicMock(name='child')

    w.metahandler.ev_written(fake_child, 'node1', 'stdout', 7)

    assert recording_handler.ev_written_calls == [('node1', 'stdout', 7)]


def test_metaworker_ev_close_timedout_iterates_keys_and_marks_worker(make_worker):
    """Hits BOTH 100-101 AND 512-515 via a single call path:

       MetaWorkerEventHandler.ev_close(child, timedout=True)
         -> for node in NodeSet._fromlist1(child.iter_keys_timeout()):
              metaworker._on_node_timeout(node)        <-- 101
                -> DistantWorker._on_node_timeout(self, node)
                -> self._close_count += 1              <-- 514
                -> self._has_timeout = True            <-- 515

    handler=None avoids any ev_close dispatch downstream so the
    assertion surface stays tight on what we want to verify.
    """
    w = make_worker(handler=None)

    fake_child = MagicMock(name='child')
    fake_child.iter_keys_timeout.return_value = ['n1', 'n2']

    w.metahandler.ev_close(fake_child, timedout=True)

    assert w._has_timeout is True
    assert w._close_count == 2  # one increment per timed-out key


def test_metaworker_ev_close_not_timedout_skips_iteration(make_worker):
    """Negative branch: when timedout=False, the iteration must be
    skipped. _has_timeout must NOT flip to True and _close_count
    must NOT increment from this call alone.
    """
    w = make_worker(handler=None)

    fake_child = MagicMock(name='child')
    # If the code path were buggy and asked for keys, this would
    # accidentally pass; force MagicMock to record any access so we
    # can assert iter_keys_timeout was NOT called.
    fake_child.iter_keys_timeout.return_value = ['n1']

    w.metahandler.ev_close(fake_child, timedout=False)

    assert w._has_timeout is False
    assert w._close_count == 0
    fake_child.iter_keys_timeout.assert_not_called()


def test_on_routing_event_dispatches_to_handler_ev_routing(make_worker,
                                                            recording_handler):
    """Hits lines 518-519: _on_routing_event delegates to
    self.eh._ev_routing(self, arg)."""
    w = make_worker(handler=recording_handler)

    arg = {"event": "reroute", "gateway": "gw1", "targets": "node[1-3]"}
    w._on_routing_event(arg)

    assert recording_handler.routing_calls == [arg]

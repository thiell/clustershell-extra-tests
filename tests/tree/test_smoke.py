"""
Smoke tests for the local conftest fixtures.

If any of these fail, the fixture infrastructure has a bug, and the
rest of the local-coverage suite won't run reliably.

Also includes the single most valuable behavioral test from PR #615:
proving that _check_ini's `if self._aborted: break` actually works
when a handler aborts the worker mid-flush. This is the line that
isn't covered by the upstream suite, and the line we cleaned up.
"""


def test_make_worker_constructs(make_worker):
    w = make_worker()
    assert w is not None
    assert w.eh is None
    # FakeTask / FakeRouter were spliced in:
    assert w.task.__class__.__name__ == 'FakeTask'
    assert w.router.__class__.__name__ == 'FakeRouter'
    # New state from PR #615:
    assert w._initialized is False
    assert w._aborted is False
    assert w._pending_pickups == []


def test_make_worker_with_handler(make_worker, recording_handler):
    w = make_worker(handler=recording_handler)
    assert w.eh is recording_handler


def test_emit_pickup_with_no_handler_short_circuits(make_worker):
    w = make_worker(handler=None)
    w._emit_pickup('node1')
    # eh is None -> early return; no state mutated
    assert w._pending_pickups == []


def test_check_ini_inner_break_when_handler_aborts_mid_flush(
        make_worker, make_abort_on_nth_pickup_handler):
    """Cover lib/ClusterShell/Worker/Tree.py line 553 (the cleanup).

    Set up:
      - a handler whose ev_pickup aborts the worker on the 2nd call
      - 4 nodes already buffered in _pending_pickups
      - _start_count high enough that _check_ini fires ev_start + flush
    Expected:
      - ev_start fires once
      - ev_pickup fires for nodes 1 and 2 (call 2 triggers abort)
      - the inner `if self._aborted: break` in _check_ini stops the loop
      - nodes 3 and 4 never see ev_pickup
      - _pending_pickups is cleared (the swap at the top of the block
        already moved everything into the local 'pending' variable)
    """
    handler = make_abort_on_nth_pickup_handler(2)
    w = make_worker(handler=handler)

    # Pre-load the pending queue. Order matters: list is FIFO.
    w._pending_pickups = ['nA', 'nB', 'nC', 'nD']
    # Ensure _check_ini doesn't bail early on the "child not started" guard
    w._child_count = 0  # _start_count >= _child_count -> True
    w._start_count = 0

    w._check_ini()

    assert handler.ev_start_calls == [w]
    assert handler.ev_pickup_calls == ['nA', 'nB']  # 'nC', 'nD' never fired
    assert w._aborted is True
    assert w._pending_pickups == []  # swap moved them out; abort cleared too


def test_check_ini_aborts_in_ev_start_no_pickups_fire(make_worker,
                                                     abort_on_start_handler):
    """Cover the case where ev_start aborts the worker BEFORE the flush loop.

    Expected behavior: the inner `if self._aborted: break` triggers on
    the first iteration; no ev_pickup fires.
    """
    handler = abort_on_start_handler
    w = make_worker(handler=handler)

    w._pending_pickups = ['nA', 'nB', 'nC']
    w._child_count = 0
    w._start_count = 0

    w._check_ini()

    assert handler.ev_start_calls == [w]
    assert handler.ev_pickup_calls == []
    assert w._aborted is True

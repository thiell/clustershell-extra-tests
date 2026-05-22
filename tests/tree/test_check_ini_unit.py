"""
Direct unit tests for TreeWorker._check_ini (refactored in PR #615).

_check_ini fires ev_start exactly once when all children have started,
then flushes pending pickups in order, respecting any mid-flush abort.

State machine:
    if (self.eh is not None and not self._initialized
            and self._start_count >= self._child_count):
        self._initialized = True
        self.eh.ev_start(self)
        pending, self._pending_pickups = self._pending_pickups, []
        for node in pending:
            if self._aborted:
                break                               # <-- the cleanup line
            fire ev_pickup(node)

Smoke covers the two abort timings already (in ev_start, mid-flush).
This file covers the guards and ordering invariants.
"""


def test_check_ini_no_handler_is_noop(make_worker):
    w = make_worker(handler=None)
    w._pending_pickups = ['nA']
    w._start_count = 1
    w._child_count = 1

    w._check_ini()

    assert w._initialized is False
    # nothing fired (no handler) and the queue is untouched
    assert w._pending_pickups == ['nA']


def test_check_ini_already_initialized_is_noop(make_worker, recording_handler):
    w = make_worker(handler=recording_handler)
    w._initialized = True
    w._pending_pickups = ['nA']  # would have been residue
    w._start_count = 1
    w._child_count = 1

    w._check_ini()

    # ev_start did NOT fire again
    assert recording_handler.ev_start_calls == []
    # queue untouched (the flush block didn't run)
    assert w._pending_pickups == ['nA']


def test_check_ini_start_count_below_child_count_is_noop(make_worker, recording_handler):
    """ev_start must only fire after every child worker has started."""
    w = make_worker(handler=recording_handler)
    w._start_count = 1
    w._child_count = 3  # 2 children haven't started yet
    w._pending_pickups = ['nA']

    w._check_ini()

    assert recording_handler.ev_start_calls == []
    assert w._initialized is False
    assert w._pending_pickups == ['nA']


def test_check_ini_happy_path_fires_start_then_pickups_in_order(make_worker, recording_handler):
    w = make_worker(handler=recording_handler)
    w._pending_pickups = ['nA', 'nB', 'nC']
    w._start_count = 0
    w._child_count = 0  # threshold reached

    w._check_ini()

    assert w._initialized is True
    assert recording_handler.ev_start_calls == [w]
    assert recording_handler.ev_pickup_calls == [
        (w, 'nA'), (w, 'nB'), (w, 'nC'),
    ]
    assert w._pending_pickups == []
    assert w._aborted is False


def test_check_ini_idempotent_on_second_call(make_worker, recording_handler):
    """Calling _check_ini a second time after the first must NOT
    re-fire ev_start, even if new pending pickups arrived in between
    (the _initialized guard owns this invariant)."""
    w = make_worker(handler=recording_handler)
    w._pending_pickups = ['nA']
    w._start_count = 0
    w._child_count = 0

    w._check_ini()
    assert recording_handler.ev_start_calls == [w]

    # A second call after some new pending arrived must NOT re-flush
    # via _check_ini (post-init pickups go through _emit_pickup's
    # direct-fire path; _check_ini is now a no-op).
    w._pending_pickups = ['nB']  # simulated residue
    w._check_ini()

    # ev_start fired exactly once total
    assert recording_handler.ev_start_calls == [w]
    # _check_ini did not touch the residue (post-init path handles it)
    assert w._pending_pickups == ['nB']


def test_check_ini_empty_pending_still_fires_ev_start(make_worker, recording_handler):
    """Even with nothing buffered, ev_start must fire when the
    threshold is reached. (This is the gateway-only-targets case
    where pickups come later from _execute_remote.)"""
    w = make_worker(handler=recording_handler)
    assert w._pending_pickups == []  # initial state
    w._start_count = 0
    w._child_count = 0

    w._check_ini()

    assert recording_handler.ev_start_calls == [w]
    assert recording_handler.ev_pickup_calls == []
    assert w._initialized is True

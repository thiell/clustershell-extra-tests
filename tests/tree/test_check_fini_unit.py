"""
Direct unit tests for TreeWorker._check_fini.

State machine:
    if self._close_count >= self._target_count:
        handler = self.eh
        if handler:
            if self._has_timeout and hasattr(handler, 'ev_timeout'):
                handler.ev_timeout(self)
            handler.ev_close(self, self._has_timeout)

    if gateway:
        targets = self.gwtargets[str(gateway)]
        if not targets:
            self.task._pchannel_release(gateway, self)
            del self.gwtargets[str(gateway)]
"""


def test_check_fini_below_target_count_is_noop(make_worker, recording_handler):
    w = make_worker(handler=recording_handler)
    w._close_count = 0
    w._target_count = 3

    w._check_fini()

    assert recording_handler.ev_close_calls == []
    assert recording_handler.ev_timeout_calls == []


def test_check_fini_at_target_count_fires_ev_close(make_worker, recording_handler):
    w = make_worker(handler=recording_handler)
    w._close_count = 3
    w._target_count = 3
    w._has_timeout = False

    w._check_fini()

    assert recording_handler.ev_close_calls == [False]
    assert recording_handler.ev_timeout_calls == []


def test_check_fini_with_timeout_fires_ev_timeout_then_ev_close(make_worker, recording_handler):
    w = make_worker(handler=recording_handler)
    w._close_count = 3
    w._target_count = 3
    w._has_timeout = True

    w._check_fini()

    # ev_timeout fires before ev_close (verifiable order)
    assert recording_handler.ev_timeout_calls == [w]
    assert recording_handler.ev_close_calls == [True]


def test_check_fini_legacy_handler_without_ev_timeout_skips_it(make_worker,
                                                               handler_without_timeout):
    """The hasattr() guard exists because ev_timeout was missing in
    ClusterShell 1.8.0. A handler lacking ev_timeout must still get
    ev_close — just no ev_timeout."""
    w = make_worker(handler=handler_without_timeout)
    w._close_count = 3
    w._target_count = 3
    w._has_timeout = True

    w._check_fini()  # must not raise

    assert handler_without_timeout.ev_close_calls == [True]


def test_check_fini_no_handler_skips_event_dispatch(make_worker):
    w = make_worker(handler=None)
    w._close_count = 3
    w._target_count = 3

    # Must not raise; nothing observable to assert beyond non-crash.
    w._check_fini()


def test_check_fini_gateway_with_remaining_targets_does_not_release(make_worker, fake_task):
    w = make_worker(handler=None)
    # Pre-set: first branch must be False so we isolate the gateway block.
    w._close_count = 0
    w._target_count = 1
    w.gwtargets['gw1'] = {'n1', 'n2'}  # still has active targets

    w._check_fini(gateway='gw1')

    assert fake_task.released == []
    assert 'gw1' in w.gwtargets


def test_check_fini_gateway_with_no_targets_releases_and_deletes(make_worker, fake_task):
    w = make_worker(handler=None)
    w._close_count = 0
    w._target_count = 1
    w.gwtargets['gw1'] = set()  # all targets have closed

    w._check_fini(gateway='gw1')

    assert fake_task.released == ['gw1']
    assert 'gw1' not in w.gwtargets

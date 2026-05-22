"""
Direct unit tests for TreeWorker._relaunch (branch 428->432).

_relaunch is called after a gateway failure: it redistributes the
targets that were on the failed gateway and triggers a fresh _launch
on them. Along the way, if a handler is present, it fires the
_ev_routing event.

  if self.eh is not None:               # <-- 428
      self.eh._ev_routing(self, {...})
  self._launch(targets)                  # <-- 432

We need to cover the False side of line 428 (handler is None).
"""


def test_relaunch_without_handler_skips_ev_routing(make_worker, fake_router):
    """Hits the False side of `if self.eh is not None:` in _relaunch.

    Setup: pre-populate gwtargets with a 'failed' gateway and trigger
    _relaunch. With handler=None, the _ev_routing dispatch must NOT be
    attempted (it would AttributeError on None.eh otherwise).
    """
    w = make_worker(handler=None)
    w.gwtargets['old-gw'] = {'node1', 'node2'}
    w._target_count = 2

    # _relaunch ends by calling self._launch(targets). Make sure that
    # is a no-op by leaving fake_router's dispatch empty.
    fake_router.set_dispatch([])

    # Must complete without crashing.
    w._relaunch('old-gw')

    # The gateway entry has been cleared by the in-between _check_fini.
    assert 'old-gw' not in w.gwtargets
    # _target_count was decremented by len(targets)
    assert w._target_count == 0


def test_relaunch_with_handler_fires_ev_routing(make_worker, recording_handler, fake_router):
    """Positive branch (handler present) — for symmetry. Also
    documents the payload shape that handlers receive."""
    w = make_worker(handler=recording_handler)
    w.gwtargets['old-gw'] = {'node1'}
    w._target_count = 1

    fake_router.set_dispatch([])

    w._relaunch('old-gw')

    # one _ev_routing call with the expected payload shape
    assert len(recording_handler.routing_calls) == 1
    arg = recording_handler.routing_calls[0]
    assert arg['event'] == 'reroute'
    assert arg['gateway'] == 'old-gw'
    assert str(arg['targets']) == 'node1'

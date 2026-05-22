"""
Direct unit tests for TreeWorker._emit_pickup.

_emit_pickup is the central choke-point through which all pickup
events flow. After the deeper fix (#594), its state machine is:

    if self.eh is None or self._aborted:
        return                                      # short-circuit
    if self._initialized:
        fire ev_pickup                              # direct
    else:
        self._pending_pickups.append(node)          # buffered

The "called at most once per node" invariant is NOT enforced inside
_emit_pickup itself -- it is held STRUCTURALLY by the two callers
(MetaWorkerEventHandler.ev_pickup for direct children, and
PropagationChannel._send_ctl for gateway-routed targets). The
upstream test test_tree_run_gw2f1_no_double_pickup_under_reroute
guards that invariant at the integration level.
"""


def test_emit_pickup_no_handler_short_circuits(make_worker):
    w = make_worker(handler=None)
    w._emit_pickup('node1')
    assert w._pending_pickups == []


def test_emit_pickup_when_aborted_short_circuits(make_worker, recording_handler):
    w = make_worker(handler=recording_handler)
    w._aborted = True

    w._emit_pickup('node1')

    assert w._pending_pickups == []
    assert recording_handler.ev_pickup_calls == []


def test_emit_pickup_before_init_buffers(make_worker, recording_handler):
    """Pre-ev_start pickups are queued, not fired immediately."""
    w = make_worker(handler=recording_handler)
    assert w._initialized is False

    w._emit_pickup('node1')
    w._emit_pickup('node2')

    # queued, not fired
    assert w._pending_pickups == ['node1', 'node2']
    assert recording_handler.ev_pickup_calls == []


def test_emit_pickup_after_init_fires_directly(make_worker, recording_handler):
    """Post-ev_start pickups go straight to the handler."""
    w = make_worker(handler=recording_handler)
    w._initialized = True

    w._emit_pickup('node1')

    # fired, not queued
    assert w._pending_pickups == []
    assert recording_handler.ev_pickup_calls == [(w, 'node1')]

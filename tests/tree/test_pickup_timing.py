"""
Local-only regression test: ev_pickup timing on gateway-down (#594).

Background
----------
With PR #615 alone, _emit_pickup() fired from TreeWorker._execute_remote
and _copy_remote at the moment pchan.shell(...) returned -- whether or
not the propagation channel had actually written the CTL on the wire.
For a target whose initial gateway is unreachable, the CTL is queued
in PropagationChannel._sendq but never sent until reroute; ev_pickup
fired prematurely.

The Option B follow-up plumbs (pickup_worker, pickup_nodes) through
PropagationChannel.send_queued() / send_dequeue(), so _emit_pickup is
invoked from _send_ctl() AFTER the actual send -- meaning pickup
semantics now match "command really left the local process".

What this test asserts
----------------------
For the gw2f1 topology (one ok gateway + one unreachable), the
ev_pickup for the rerouted target must come AFTER the _ev_routing
event. Also prints the full event sequence -- useful demo material
to share with reviewers.

Run me:
    pytest -s tests/tree/test_pickup_timing.py

The -s flag is important: the demo prints the event sequence to stdout.
"""

import socket
import unittest

from ClusterShell.Event import EventHandler
from ClusterShell.NodeSet import NodeSet
from ClusterShell.Task import task_self, task_terminate
from ClusterShell.Topology import TopologyGraph


HOSTNAME = socket.gethostname().split('.', 1)[0]
NODE_GATEWAY2F1 = '127.0.0.6,192.0.2.0'   # one ok, one (RFC 5737) unreachable
NODE_DISTANT2 = '127.0.0.[2-3]'


class SequenceRecordingHandler(EventHandler):
    """Capture the order of events, with per-event payload."""

    def __init__(self):
        super().__init__()
        self.events = []   # list of (event_name, payload) tuples

    def ev_start(self, worker):
        self.events.append(('start', None))

    def ev_pickup(self, worker, node):
        self.events.append(('pickup', str(node)))

    def ev_read(self, worker, node, sname, msg):
        # Truncate msg for readable demo output
        s = msg if len(msg) <= 32 else msg[:29] + b'...'
        self.events.append(('read', (str(node), sname, s)))

    def ev_hup(self, worker, node, rc):
        self.events.append(('hup', (str(node), rc)))

    def ev_close(self, worker, timedout):
        self.events.append(('close', timedout))

    def _ev_routing(self, worker, arg):
        # Stringify payload bits so the demo print is readable
        self.events.append(('routing', {
            'event': arg['event'],
            'gateway': arg['gateway'],
            'targets': str(arg['targets']),
        }))


@unittest.skipIf(HOSTNAME == 'localhost',
                 "does not work with hostname set to 'localhost'")
class PickupTimingOnGWDownTest(unittest.TestCase):
    """Topology: HOSTNAME -> {127.0.0.6 (ok), 192.0.2.0 (fail)} -> 127.0.0.[2-3]"""

    def setUp(self):
        task_terminate()
        self.task = task_self()
        graph = TopologyGraph()
        graph.add_route(NodeSet(HOSTNAME), NodeSet(NODE_GATEWAY2F1))
        graph.add_route(NodeSet(NODE_GATEWAY2F1), NodeSet(NODE_DISTANT2))
        self.task.topology = graph.to_tree(HOSTNAME)

    def tearDown(self):
        task_terminate()

    def test_pickup_fires_after_routing_for_rerouted_target(self):
        """ev_pickup for the rerouted target must follow _ev_routing.

        With the Option B fix in PropagationChannel, _emit_pickup fires
        only when the CTL actually leaves the local process. So for the
        target whose initial gateway is unreachable, ev_pickup is delayed
        until the reroute lands its send on a working gateway.
        """
        teh = SequenceRecordingHandler()
        self.task.run('echo Lorem Ipsum', nodes=NODE_DISTANT2, handler=teh)

        # ---- show-case demo: print the full sequence ----
        print()
        print('=' * 64)
        print(' Event sequence (gw2f1 topology, gateway down, deeper fix)')
        print('=' * 64)
        for i, (name, payload) in enumerate(teh.events):
            print('  %2d.  %-9s  %r' % (i, name, payload))
        print('=' * 64)

        # ---- basic invariants that should hold either way ----
        starts = [i for i, e in enumerate(teh.events) if e[0] == 'start']
        pickups = [(i, p) for i, e in enumerate(teh.events) for p in [e[1]] if e[0] == 'pickup']
        routings = [(i, e[1]) for i, e in enumerate(teh.events) if e[0] == 'routing']
        closes = [i for i, e in enumerate(teh.events) if e[0] == 'close']

        self.assertEqual(len(starts), 1, 'ev_start should fire exactly once')
        self.assertEqual(len(pickups), 2,
                         'ev_pickup_cnt invariant: one per node, no double-fire on reroute')
        self.assertEqual(len(routings), 1,
                         'exactly one reroute event for the down gateway')
        self.assertEqual(len(closes), 1, 'ev_close should fire exactly once')

        # ---- timing invariant: pickup of rerouted target follows routing ----
        # With the Option B fix, _emit_pickup is fired by PropagationChannel
        # only when the CTL actually leaves the local process. So the
        # pickup for the rerouted target must come AFTER the _ev_routing
        # that triggered the new send via a working gateway.
        routing_idx, routing_arg = routings[0]
        rerouted = NodeSet(routing_arg['targets'])

        print()
        print('  rerouted target(s):    %s' % rerouted)
        print('  routing event index:   %d' % routing_idx)
        for tgt in rerouted:
            tgt_str = str(tgt)
            for pidx, pnode in pickups:
                if pnode == tgt_str:
                    print('  pickup(%s) index:  %d   (%s routing)' % (
                        tgt_str, pidx,
                        'AFTER' if pidx > routing_idx else 'BEFORE'))
                    self.assertGreater(
                        pidx, routing_idx,
                        "pickup for rerouted target %s preceded routing -- "
                        "the deeper PropagationChannel fix has regressed; "
                        "ev_pickup should fire only when the CTL actually "
                        "leaves the local process (#594)." % tgt_str,
                    )
        print()

"""
Local-only call-count probe for the #594 structural invariant.

Stronger than the upstream regression test
(test_tree_run_gw2f1_no_double_pickup_under_reroute): instead of
asserting on the handler-visible pickup count, this probe instruments
TreeWorker._emit_pickup directly and counts CALLS per node-key. If a
future refactor reintroduces eager pickup emission from
TreeWorker._execute_remote / _copy_remote (or any other pre-send
site), the call counter trips here before any handler dispatch is
even considered.

The invariant under test:

    For any node N and any worker W, TreeWorker._emit_pickup(N) is
    invoked at most once during W's lifetime.

Why it holds after the deeper fix (#594):

  Direct-child path:
    Worker._on_start(key) calls metahandler.ev_pickup(child, key)
    once per engine client; the metahandler then calls
    metaworker._emit_pickup(N) -- once per node-key.

  Gateway-routed path:
    PropagationChannel._send_ctl(ctl, worker, nodes) is invoked
    exactly once per actual channel.send(ctl). Each shell() call
    produces one CTL; that CTL takes the immediate-send path OR the
    queued path, never both; the queue dispatches via send_dequeue
    exactly once. A CTL queued behind a CFG-ACK that never arrives
    is GC'd with the channel before any send -- so its targets'
    _emit_pickup is never called. Reroute issues a FRESH shell() on
    a working gateway whose _send_ctl fires _emit_pickup for the
    first (and only) time.
"""

import socket
import unittest
from collections import Counter

from ClusterShell.Event import EventHandler
from ClusterShell.NodeSet import NodeSet
from ClusterShell.Task import task_self, task_terminate
from ClusterShell.Topology import TopologyGraph
from ClusterShell.Worker import Tree as TreeMod


HOSTNAME = socket.gethostname().split('.', 1)[0]
NODE_GATEWAY2F1 = '127.0.0.6,192.0.2.0'   # one ok, one unreachable
NODE_DISTANT2 = '127.0.0.[2-3]'


class NoOpHandler(EventHandler):
    """Minimal handler so the worker has self.eh set, but with no logic.

    We are probing _emit_pickup CALL counts, not handler dispatch. The
    handler is required only so _emit_pickup doesn't early-return on
    'eh is None'.
    """
    pass


@unittest.skipIf(HOSTNAME == 'localhost',
                 "does not work with hostname set to 'localhost'")
class EmitPickupAtMostOnceTest(unittest.TestCase):
    """Topology: HOSTNAME -> {127.0.0.6 (ok), 192.0.2.0 (fail)} -> 127.0.0.[2-3]"""

    def setUp(self):
        task_terminate()
        self.task = task_self()
        graph = TopologyGraph()
        graph.add_route(NodeSet(HOSTNAME), NodeSet(NODE_GATEWAY2F1))
        graph.add_route(NodeSet(NODE_GATEWAY2F1), NodeSet(NODE_DISTANT2))
        self.task.topology = graph.to_tree(HOSTNAME)

        # Instrument _emit_pickup with a call counter
        self._counts = Counter()
        self._orig_emit_pickup = TreeMod.TreeWorker._emit_pickup

        counts = self._counts
        orig = self._orig_emit_pickup
        def counting_emit_pickup(s, node):
            counts[str(node)] += 1
            return orig(s, node)
        TreeMod.TreeWorker._emit_pickup = counting_emit_pickup

    def tearDown(self):
        # Restore _emit_pickup BEFORE task_terminate (which may still
        # try to call into worker methods).
        TreeMod.TreeWorker._emit_pickup = self._orig_emit_pickup
        task_terminate()

    def test_emit_pickup_called_at_most_once_per_node_under_reroute(self):
        """Probe: _emit_pickup invoked exactly once per node, even with
        a failed-gateway reroute."""
        teh = NoOpHandler()
        self.task.run('echo hi', nodes=NODE_DISTANT2, handler=teh)

        over_one = {n: c for n, c in self._counts.items() if c > 1}
        self.assertFalse(
            over_one,
            "_emit_pickup called more than once for: %s "
            "(full counts: %s)" % (over_one, dict(self._counts)))

        # Sanity: each distant target was picked up exactly once.
        expected = set(str(n) for n in NodeSet(NODE_DISTANT2))
        self.assertEqual(set(self._counts.keys()), expected,
                         "missing pickup for some target; counts=%s"
                         % dict(self._counts))

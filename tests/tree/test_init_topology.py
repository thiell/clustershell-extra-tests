"""
Direct unit tests for TreeWorker.__init__ explicit topology= kwarg
(lines 213-215).

This branch fires only when the caller passes `topology=` as a kwarg
to TreeWorker directly. The integration tests don't take this path —
they let the worker pick up the task's default topology via _start.

  if self.topology is not None:
      self.newroot = kwargs.get('newroot') or \
                     str(self.topology.root.nodeset)
      self.router = PropagationTreeRouter(self.newroot, self.topology)
  else:
      self.router = None

We monkeypatch PropagationTreeRouter to avoid pulling in real
topology infrastructure — we just need to confirm it's invoked with
the right arguments.
"""

from unittest.mock import MagicMock

from ClusterShell.NodeSet import NodeSet
from ClusterShell.Worker.Tree import TreeWorker


def test_init_with_topology_kwarg_derives_newroot_and_builds_router(monkeypatch):
    """Hits lines 213-215 when caller passes topology= and NOT newroot=.

    newroot is derived from str(topology.root.nodeset), then
    PropagationTreeRouter is instantiated with (newroot, topology).
    """
    fake_router_class = MagicMock(name='PropagationTreeRouter')
    monkeypatch.setattr(
        'ClusterShell.Worker.Tree.PropagationTreeRouter',
        fake_router_class,
    )

    fake_topo = MagicMock(name='topology')
    fake_topo.root.nodeset = NodeSet('gw1')

    w = TreeWorker(NodeSet('node1'), None, None,
                   command='true',
                   topology=fake_topo)

    fake_router_class.assert_called_once_with('gw1', fake_topo)
    assert w.newroot == 'gw1'
    assert w.router is fake_router_class.return_value
    assert w.topology is fake_topo


def test_init_explicit_newroot_overrides_topology_root(monkeypatch):
    """Explicit newroot= wins over the str(topology.root.nodeset) default."""
    fake_router_class = MagicMock(name='PropagationTreeRouter')
    monkeypatch.setattr(
        'ClusterShell.Worker.Tree.PropagationTreeRouter',
        fake_router_class,
    )

    fake_topo = MagicMock(name='topology')
    fake_topo.root.nodeset = NodeSet('gw-default')

    w = TreeWorker(NodeSet('node1'), None, None,
                   command='true',
                   topology=fake_topo,
                   newroot='gw-explicit')

    fake_router_class.assert_called_once_with('gw-explicit', fake_topo)
    assert w.newroot == 'gw-explicit'


def test_init_no_topology_kwarg_leaves_router_none():
    """The else branch — already covered by existing tests, but
    asserted explicitly for documentation."""
    w = TreeWorker(NodeSet('node1'), None, None, command='true')
    assert w.topology is None
    assert w.router is None

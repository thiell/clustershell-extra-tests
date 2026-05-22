"""
Local-only direct unit tests for PropagationChannel._send_ctl and the
deeper-fix plumbing in send_queued / send_dequeue.

Maps directly to the new code paths added by the Option B deeper fix:

  - send_queued(ctl, pickup_worker, pickup_nodes) immediate-send branch
    (setup=True AND _sendq empty)            -> Propagation.py:242
  - send_queued queued branch
    (setup=False OR _sendq non-empty)        -> Propagation.py:244-245
  - send_dequeue dispatches via _send_ctl    -> Propagation.py:250-252
  - _send_ctl fires _emit_pickup AFTER send  -> Propagation.py:233-236
  - _send_ctl with no pickup args (write/eof
    paths) does NOT fire pickup              -> Propagation.py:234 (False side)

The immediate-send path is reachable in real life when a subsequent
shell() arrives on an already-setup channel with a drained queue, but
is not exercised by the upstream integration suite (each test uses a
fresh channel that starts in cfg state). These tests close that gap.
"""

from collections import deque
from unittest.mock import MagicMock

import pytest

from ClusterShell.NodeSet import NodeSet
from ClusterShell.Propagation import PropagationChannel


@pytest.fixture
def channel():
    """A PropagationChannel constructed without a real Task or gateway.

    We never call .start() / .recv(), so we never need the upstream
    Channel machinery to be fully wired. We only exercise the queue
    helpers: send_queued, send_dequeue, _send_ctl.
    """
    fake_task = MagicMock(name='Task')
    chan = PropagationChannel(fake_task, gateway='gw1')
    # Replace .send() with a recorder so we don't try to talk to a real
    # network. send() is the network-facing method; everything we test
    # sits above it.
    chan.send = MagicMock(name='send')
    return chan


@pytest.fixture
def worker_recorder():
    """A worker whose _emit_pickup records every call."""
    w = MagicMock(name='worker')
    w._emit_pickup = MagicMock(name='_emit_pickup')
    return w


def _pickup_nodes(worker_recorder):
    return [str(c.args[0]) for c in worker_recorder._emit_pickup.call_args_list]


# ---------------------------------------------------------------- send_queued

def test_send_queued_immediate_path_sends_and_fires_pickups(channel, worker_recorder):
    """Hits Propagation.py:242 — setup=True AND _sendq empty: send and
    fire pickup synchronously, one per node, AFTER send."""
    channel.setup = True
    assert len(channel._sendq) == 0

    ctl = object()                              # opaque CTL placeholder
    nodes = NodeSet('n[1-3]')

    channel.send_queued(ctl, pickup_worker=worker_recorder, pickup_nodes=nodes)

    # The CTL went out on the wire exactly once.
    channel.send.assert_called_once_with(ctl)
    # One ev_pickup per node, in NodeSet iteration order.
    assert _pickup_nodes(worker_recorder) == ['n1', 'n2', 'n3']
    # Queue stayed empty.
    assert len(channel._sendq) == 0


def test_send_queued_immediate_path_pickup_fires_after_send(channel, worker_recorder):
    """Ordering invariant: send() must run BEFORE _emit_pickup, so the
    CTL is on the wire before handlers observe ev_pickup."""
    order = []
    channel.send.side_effect = lambda ctl: order.append('send')
    worker_recorder._emit_pickup.side_effect = lambda node: order.append('pickup:%s' % node)

    channel.setup = True
    channel.send_queued(object(), pickup_worker=worker_recorder,
                        pickup_nodes=NodeSet('n1'))

    assert order == ['send', 'pickup:n1']


def test_send_queued_queued_path_when_not_setup(channel, worker_recorder):
    """Hits Propagation.py:244-245 — setup=False: must queue and NOT
    send or fire pickup until send_dequeue runs."""
    channel.setup = False                       # CFG-ACK not yet received

    ctl = object()
    nodes = NodeSet('n1,n2')

    channel.send_queued(ctl, pickup_worker=worker_recorder, pickup_nodes=nodes)

    channel.send.assert_not_called()
    worker_recorder._emit_pickup.assert_not_called()
    assert len(channel._sendq) == 1
    queued_ctl, queued_worker, queued_nodes = channel._sendq[0]
    assert queued_ctl is ctl
    assert queued_worker is worker_recorder
    assert queued_nodes is nodes


def test_send_queued_queued_path_when_setup_but_sendq_nonempty(channel, worker_recorder):
    """The setup=True path still defers when _sendq is non-empty: we
    must preserve in-order delivery."""
    channel.setup = True
    channel._sendq = deque([('prior-ctl', None, None)])

    ctl = object()
    channel.send_queued(ctl, pickup_worker=worker_recorder,
                        pickup_nodes=NodeSet('n1'))

    channel.send.assert_not_called()
    worker_recorder._emit_pickup.assert_not_called()
    # New tuple was appended LEFT (deque) — it'll be popped after the prior.
    assert len(channel._sendq) == 2
    assert channel._sendq[0] == (ctl, worker_recorder, NodeSet('n1'))
    assert channel._sendq[-1] == ('prior-ctl', None, None)


def test_send_queued_without_pickup_args_does_not_emit(channel):
    """write() and set_write_eof() call send_queued(ctl) with no pickup
    args. _send_ctl must NOT try to iterate or call _emit_pickup."""
    channel.setup = True
    ctl = object()

    channel.send_queued(ctl)  # no pickup_worker/pickup_nodes

    channel.send.assert_called_once_with(ctl)
    # Nothing to assert on a worker -- the point is: this must not raise


# --------------------------------------------------------------- send_dequeue

def test_send_dequeue_fires_pickups(channel, worker_recorder):
    """Hits Propagation.py:250-252 — drain one queued entry: send it
    and fire pickups."""
    channel._sendq.appendleft(
        ('queued-ctl', worker_recorder, NodeSet('a,b')))

    channel.send_dequeue()

    channel.send.assert_called_once_with('queued-ctl')
    assert _pickup_nodes(worker_recorder) == ['a', 'b']
    assert len(channel._sendq) == 0


def test_send_dequeue_without_pickup_args(channel):
    """A previously-queued write or eof CTL has no pickup args; dequeue
    must send and NOT try to fire pickup."""
    channel._sendq.appendleft(('queued-write', None, None))

    channel.send_dequeue()                      # must not raise

    channel.send.assert_called_once_with('queued-write')
    assert len(channel._sendq) == 0


def test_send_dequeue_empty_queue_is_noop(channel):
    """Defensive: dequeue with empty _sendq is a no-op."""
    assert len(channel._sendq) == 0

    channel.send_dequeue()

    channel.send.assert_not_called()


def test_send_dequeue_drains_one_at_a_time(channel, worker_recorder):
    """send_dequeue is meant to be called once per ACK — each call
    pops exactly one entry (FIFO order via deque.pop)."""
    channel._sendq.appendleft(('first', worker_recorder, NodeSet('n1')))
    channel._sendq.appendleft(('second', worker_recorder, NodeSet('n2')))

    # First call dequeues 'first' (it was appended LEFT first, then 'second')
    channel.send_dequeue()
    assert channel.send.call_args_list[-1].args == ('first',)
    assert _pickup_nodes(worker_recorder) == ['n1']

    channel.send_dequeue()
    assert channel.send.call_args_list[-1].args == ('second',)
    assert _pickup_nodes(worker_recorder) == ['n1', 'n2']

    assert len(channel._sendq) == 0


# ------------------------------------------------------------------ _send_ctl

def test_send_ctl_with_pickup_args_fires_after_send(channel, worker_recorder):
    """_send_ctl directly: send() first, then _emit_pickup per node."""
    order = []
    channel.send.side_effect = lambda ctl: order.append(('send', ctl))
    worker_recorder._emit_pickup.side_effect = (
        lambda node: order.append(('pickup', str(node))))

    channel._send_ctl('payload', worker_recorder, NodeSet('x,y'))

    assert order == [('send', 'payload'), ('pickup', 'x'), ('pickup', 'y')]


def test_send_ctl_no_pickup_args_skips_emit(channel):
    """_send_ctl with pickup_worker=None must NOT iterate pickup_nodes."""
    channel._send_ctl('payload', None, None)
    channel.send.assert_called_once_with('payload')


def test_send_ctl_pickup_worker_set_but_no_nodes_is_safe(channel, worker_recorder):
    """Belt-and-braces: pickup_worker set but pickup_nodes None should
    not crash (defensive)."""
    channel._send_ctl('payload', worker_recorder, None)
    channel.send.assert_called_once_with('payload')
    worker_recorder._emit_pickup.assert_not_called()

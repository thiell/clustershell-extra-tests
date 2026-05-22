"""
Direct unit tests for TreeWorker.write() OSError handling.

write() has three states:
  - pre-started: buffer the write via self._port.msg_send and return
  - post-started, no failure: each child .write() succeeds; ends silently
  - post-started, some child raised OSError: collect the LAST OSError,
    still flush to _write_remote, then re-raise at the end

The 'collect and re-raise' design exists so a partial-failure batch
does NOT silently drop data to the surviving children.
"""

import pytest
from unittest.mock import MagicMock


def test_write_post_started_oserror_collected_and_reraised(make_worker):
    """Hits lines 602-603 (except catch) and 608 (raise osexc).

    Even with a failing child, _write_remote must still run (we set
    gwtargets empty so it's a verifiable no-op), and the OSError must
    bubble out at the end.
    """
    w = make_worker()
    w._started = True

    failing = MagicMock(name='failing-child')
    failing.write.side_effect = OSError("simulated write failure")
    w.workers = [failing]

    with pytest.raises(OSError, match='simulated write failure'):
        w.write(b'payload')

    failing.write.assert_called_once_with(b'payload')


def test_write_post_started_all_children_succeed_no_raise(make_worker):
    """Negative branch of `if osexc:` — every child writes cleanly,
    so write() returns without raising and all children received the
    payload."""
    w = make_worker()
    w._started = True

    ok1 = MagicMock(name='ok1')
    ok2 = MagicMock(name='ok2')
    w.workers = [ok1, ok2]

    w.write(b'payload')  # must NOT raise

    ok1.write.assert_called_once_with(b'payload')
    ok2.write.assert_called_once_with(b'payload')


def test_write_oserror_does_not_skip_remote_flush(make_worker):
    """The flush to remote workers must happen even when a direct
    child raised. This is the load-bearing invariant of the catch:
    'collect, keep going, raise at end' — not 'raise immediately'.
    """
    w = make_worker()
    w._started = True

    failing = MagicMock(name='failing-direct')
    failing.write.side_effect = OSError("kaboom")
    w.workers = [failing]

    # Spy on _write_remote so we know it ran despite the OSError.
    flushes = []
    w._write_remote = lambda buf: flushes.append(buf)

    with pytest.raises(OSError, match='kaboom'):
        w.write(b'mybytes')

    assert flushes == [b'mybytes']

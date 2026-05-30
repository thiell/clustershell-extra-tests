# Tree.py — findings from coverage-driven testing

Notes capturing latent issues in `lib/ClusterShell/Worker/Tree.py`
observed while driving the module to 100% line + branch coverage
via the unit tests in this repository.

Findings are numbered within each session. When a finding is reported
or fixed upstream, mark its **Status** line with the PR or issue link
and date.

---

## Session 2026-05-20 — coverage drive on `fix/gh594-pickup-events-on-reroute`

Branch state at time of findings: local SHA `83fc658` (PR #615 amended
with the redundant-guard cleanup degremont flagged in review).

### Index

1. [Trailing base64 leftover bytes are written raw into the tar (rcopy)](#1-trailing-base64-leftover-bytes-are-written-raw-into-the-tar-rcopy)
2. [`extractall` catches only `IOError`/`OSError`, not `tarfile.TarError`](#2-extractall-catches-only-ioerroroserror-not-tarfiletarerror)
3. [`write()` keeps only the *last* `OSError` from a multi-child failure](#3-write-keeps-only-the-last-oserror-from-a-multi-child-failure)
4. [Python 3.12+ tarfile compatibility (PEP 706)](#4-python-312-tarfile-compatibility-pep-706)
5. [Smaller observations](#5-smaller-observations)

---

### 1. Trailing base64 leftover bytes are written raw into the tar (rcopy)

**Files / lines**

- `lib/ClusterShell/Worker/Tree.py:446-456` — accumulates `_rcopy_bufs`
- `lib/ClusterShell/Worker/Tree.py:473-476` — flushes the leftover at close

**Observation**

In `_on_remote_node_msgline` rcopy mode:

```python
encoded = self._rcopy_bufs.setdefault(node, b'') + msg
encoded_sz = (len(encoded) // 4) * 4
self._rcopy_tars[node].write(base64.b64decode(encoded[0:encoded_sz]))
self._rcopy_bufs[node] = encoded[encoded_sz:]   # trailing 0-3 base64 chars
```

`_rcopy_bufs[node]` is left holding 0-3 **encoded** base64 characters
because they don't form a complete 4-char group. Then `_on_remote_node_close`
does:

```python
buf = self._rcopy_bufs[node]
if len(buf) > 0:
    self.logger.debug("flushing node %s buf %d bytes", node, len(buf))
    tarfileobj.write(buf)         # writes raw base64 chars, NOT decoded
```

It writes those raw base64 characters **directly into the tarfile object**,
appending up to 3 garbage bytes after the end-of-archive blocks. In
practice `extractall` tolerates the trailing garbage and nobody has
noticed, but the code clearly intends "buf" to be decoded data — the
debug log even says "flushing N bytes".

**Severity**: low-risk but real latent bug. The rcopy stream produced
by `Worker/Tree.TAR_CMD_FMT` plus `base64 -w 65536` is always a multiple
of 4 chars, so this branch is **unreachable in normal use**. Only a
truncated stream from a network-interrupted gateway can land here, and
even then the trailing garbage typically doesn't corrupt the extracted
files (it sits past the EOF markers).

**Suggested fix**

Either drop trailing chars (they're an incomplete base64 group and can't
represent anything meaningful), or pad-and-decode them. The clearest
patch is to just drop them and log a warning:

```python
if len(buf) > 0:
    self.logger.warning(
        "node %s: discarding %d trailing base64 chars at close",
        node, len(buf),
    )
```

**Status**: not reported upstream yet. Self-contained issue, not in
scope of PR #615.

---

### 2. `extractall` catches only `IOError`/`OSError`, not `tarfile.TarError`

**File / lines**: `lib/ClusterShell/Worker/Tree.py:479-487`

**Observation**

```python
tmptar = tarfile.open(fileobj=tarfileobj)
try:
    tmptar.extractall(path=self.dest)
except IOError as ex:
    self._on_remote_node_msgline(node, ex, 'stderr', gateway)
finally:
    tmptar.close()
```

A corrupt tar payload causes `extractall` to raise
`tarfile.ReadError` — a subclass of `tarfile.TarError` which
inherits from `Exception`, **not** from `OSError`. That escapes the
`except` and crashes the worker mid-close.

The `tarfile.open(fileobj=tarfileobj)` call is also **outside** the
`try`, so a corrupted-tar that fails at open (e.g., truncated header)
crashes too.

**Severity**: medium. The intent of the existing handler is "if the
local extract fails, report it to the user as stderr from that node".
By that logic, tar-format errors should be caught the same way as
filesystem errors. A malicious or buggy remote gateway can today take
down the whole worker by streaming bad bytes.

**Suggested fix**

```python
try:
    tmptar = tarfile.open(fileobj=tarfileobj)
    tmptar.extractall(path=self.dest, filter='data')  # see finding #4
except (IOError, tarfile.TarError) as ex:
    self._on_remote_node_msgline(node, ex, 'stderr', gateway)
finally:
    try:
        tmptar.close()
    except (NameError, tarfile.TarError):
        pass
```

One-line low-risk hardening. Could be a small standalone PR.

**Status**: not reported upstream yet.

---

### 3. `write()` keeps only the *last* `OSError` from a multi-child failure

**File / lines**: `lib/ClusterShell/Worker/Tree.py:597-608`

**Observation**

```python
osexc = None
for worker in self.workers:
    try:
        worker.write(buf)
    except OSError as exc:
        osexc = exc           # silently overwrites any prior exception
self._write_remote(buf)
if osexc:
    raise osexc
```

If three children fail with three different `OSError`s, only the
third is re-raised. The first two are dropped without even a log.

**Severity**: minor. The deferred-raise pattern shows the author was
thinking about reporting failures, but the implementation only keeps
the last one. In practice large clusters with cascading I/O failures
on `write()` would see only one of the underlying errors surfaced.

**Suggested fix** (judgement call)

Option A — log each, raise the last (preserves current external behavior):

```python
for worker in self.workers:
    try:
        worker.write(buf)
    except OSError as exc:
        self.logger.error("write to %s failed: %s", worker, exc)
        osexc = exc
```

Option B (Py 3.11+) — `ExceptionGroup`:

```python
osexcs = []
for worker in self.workers:
    try:
        worker.write(buf)
    except OSError as exc:
        osexcs.append(exc)
self._write_remote(buf)
if osexcs:
    raise ExceptionGroup("write() errors", osexcs)
```

Option B is the technically correct one but bumps the minimum Python.
Until that's acceptable, option A is a clear improvement.

**Status**: not reported upstream yet.

---

### 4. Python 3.12+ tarfile compatibility (PEP 706)

**File / line**: `lib/ClusterShell/Worker/Tree.py:483`

**Observation**

Running the rcopy tests under Python 3.9 emits this `RuntimeWarning`
from cpython's `tarfile`:

```
RuntimeWarning: The default behavior of tarfile extraction has been changed
to disallow common exploits (including CVE-2007-4559). By default,
absolute/parent paths are disallowed and some mode bits are cleared.
```

Per [PEP 706](https://peps.python.org/pep-0706/), calling
`extractall(path=...)` without an explicit `filter=` argument is:
- silent in Python <= 3.11
- deprecated with `RuntimeWarning` in 3.12 and 3.13
- a `DeprecationWarning` upgraded to `TarError` in 3.14

ClusterShell's CI already exercises 3.13, so the warning is currently
benign. It will become a hard error around the time Python 3.14 ships
(scheduled ~2025-10).

**Severity**: low today, blocking on 3.14.

**Suggested fix**

```python
tmptar.extractall(path=self.dest, filter='data')
```

`filter='data'` strips unsafe modes and rejects absolute/parent paths
— the secure default. `filter='tar'` is the literal-tar-format
behavior if a user needs the old semantics. Either is forward-compatible.

**Status**: fix shipped as `filter='fully_trusted'` with feature-detection in
[thiell/clustershell@c4bcb86](https://github.com/thiell/clustershell/commit/c4bcb86)
on branch `fix/tree-extractall-pep706` (2026-05-22). `'fully_trusted'` was
chosen over `'tar'`/`'data'` because tarballs are produced by trusted
ClusterShell gateways and the secure filters would strip S_IWGRP|S_IWOTH
(downgrading e.g. 0664 to 0644). Upstream PR pending. The matching
regression test in this repo is `tests/tree/test_extractall_filter.py`.

---

### 5. Smaller observations

#### 5a. `assert self.source is None` in `_launch`

**File / line**: `lib/ClusterShell/Worker/Tree.py:311`

The `if not self.remote:` block has `assert self.source is None` as
its first statement. Calling `TreeWorker(..., remote=False, source=...)`
fails with an `AssertionError` deep inside `_launch` rather than a
clear `ValueError` at construction time. Validation belongs in
`__init__` next to the existing `"missing command or source"`
check.

#### 5b. `MetaWorkerEventHandler.ev_pickup` drops child worker identity

**File / lines**: `lib/ClusterShell/Worker/Tree.py:58-63`

```python
def ev_pickup(self, worker, node):
    """Propagate the event to the meta worker."""
    self.metaworker._emit_pickup(node)
```

The child `worker` argument is intentionally discarded — `_emit_pickup`
fires from the **meta** worker's perspective. This is by design but
worth a one-line comment so future readers don't think the drop is a
bug: e.g. `# child identity dropped: meta worker is the public handle`.

#### 5c. ~~`_emit_pickup` early-return when `self.eh is None` skips dedup bookkeeping~~ — OBSOLETED

**Status**: Resolved by removing `_picked_up_nodes` entirely. With the
deeper fix (#594), `_emit_pickup` is called structurally at most once
per node by both call sites (Worker._on_start for direct children,
PropagationChannel._send_ctl for gateway-routed). The dedup set was
load-bearing only for the eager-pickup version (pre-#594 follow-up)
where reroute could double-fire from the queued-but-never-sent CTL.
See section "Guarding the no-double-pickup invariant" below.

#### 5d. Comment on `_check_ini` flush-loop

**File / lines**: `lib/ClusterShell/Worker/Tree.py:548-554`

The comment we left after PR #615's cleanup says:

```python
# Flush pickup events buffered during _launch(). If ev_start
# aborted the worker, no pickups are emitted (#594).
```

That's accurate, but it doesn't mention the second abort timing the
loop also handles (handler calling `worker.abort()` from inside
`ev_pickup` mid-flush). Could be tightened to:

```python
# Flush pickup events buffered during _launch(). The inner break
# handles both abort timings: ev_start above, and a handler calling
# worker.abort() from inside ev_pickup mid-flush (#594).
```

Nice-to-have. Not blocking.

---

## Guarding the no-double-pickup invariant (post-dedup-removal)

`_picked_up_nodes` was removed (session 2026-05-21) because the deeper
fix in `PropagationChannel._send_ctl` makes the dedup structurally
unnecessary: a CTL queued behind a failed CFG-ACK is never sent, so
its `_emit_pickup` is never called, and reroute issues a fresh CTL on
a working channel whose `_send_ctl` fires `_emit_pickup` for the first
(and only) time.

To verify the invariant still holds after any future Tree.py /
Propagation.py change, the two regression tests we built are:

  - upstream (in `clustershell/clustershell` checkout):
    `tests/TreeWorkerTest.py::TreeWorkerGW2F1FTest::test_tree_run_gw2f1_no_double_pickup_under_reroute`
  - this repo:
    `tests/tree/test_emit_pickup_at_most_once.py`

### How to mutation-test the guard

If anyone questions whether the tests would catch a regression:

1. In `lib/ClusterShell/Worker/Tree.py`, deliberately reintroduce an
   eager pickup loop in `_execute_remote` (and/or `_copy_remote`):

   ```python
   pchan = self.task._pchannel(gateway, self)
   pchan.shell(...)
   # mutation:
   for node in targets:
       self._emit_pickup(node)
   ```

2. Run:

   ```bash
   # In a clustershell checkout, against the upstream regression test:
   pytest tests/TreeWorkerTest.py::TreeWorkerGW2F1FTest::test_tree_run_gw2f1_no_double_pickup_under_reroute -v

   # In this repo, against the local probe:
   pytest tests/tree/test_emit_pickup_at_most_once.py -v
   ```

3. Both should fail. Verified on 2026-05-21 with this exact mutation:
   - upstream test:
     `AssertionError: 5 != 2 : ev_pickup fired more than once for some node: ['127.0.0.2', '127.0.0.3', '127.0.0.2', '127.0.0.3', '127.0.0.3']`
   - local probe:
     `AssertionError: {'127.0.0.2': 2, '127.0.0.3': 3} is not false : _emit_pickup called more than once for: {'127.0.0.2': 2, '127.0.0.3': 3}`

4. Revert the mutation; both tests pass again.

---

## How to use this file

- Reference findings by their numbered section when discussing in
  issues or PRs.
- When a finding is reported / fixed upstream, update its **Status**
  line with the PR or issue link and date.
- When adding findings from a new session, start a new top-level
  `## Session YYYY-MM-DD — …` heading. Restart finding numbering
  within each session.

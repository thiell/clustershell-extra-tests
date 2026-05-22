# Pre-existing FIXME — stdin write under gw2f1 reroute

Investigation primer for a future session. The bug lives upstream and
is **outside the scope of PR #594 work** (we surfaced it incidentally
while smoke-testing the deeper fix); use this doc as a starting point
when you want to pick it up.

---

## The FIXME

`tests/TreeWorkerTest.py:974-978` (line numbers as of `c09f827`):

```python
def test_tree_run_gw2f1_write_distant(self):
    """test tree run with write(), 1/2 gateways, distant target"""
    self._tree_run_write(NODE_DISTANT)

# FIXME, issue with stdin write in gw2f1 mode
#def test_tree_run_gw2f1_write_distant2(self):
#    """test tree run with write(), 1/2 gateways, distant 2 targets"""
#    logging.basicConfig(level=logging.DEBUG)
#    self._tree_run_write(NODE_DISTANT2)

def test_tree_run_gw2f1_write_distant2_mt(self):
    """test tree run with write(), 1/2 gateways, distant 2 targets, separate thread"""
    self._tree_run_write(NODE_DISTANT2, separate_thread=True)
```

The disabled test is the only one in the family that:
- targets **multiple** distant nodes (`NODE_DISTANT2 = '127.0.0.[2-3]'`)
- runs in the **main thread** (no `separate_thread=True`)
- goes through a **failing-gateway topology** (`NODE_GATEWAY2F1 = '127.0.0.6,192.0.2.0'`)

The siblings (`_write_distant` for one target, `_write_distant2_mt` for
multi-target on a separate thread) are NOT disabled, suggesting the
issue is specific to that combination.

## What the disabled test would do

`_tree_run_write` (lines 148-171 in `TreeWorkerTest.py`):

```python
def _tree_run_write(self, target, separate_thread=False):
    """helper to write to stdin"""
    if separate_thread:
        task = Task()
    else:
        task = self.task
    teh = TEventHandler()
    worker = task.shell('cat', nodes=target, handler=teh)
    worker.write(b'Lorem Ipsum')
    worker.set_write_eof()
    task.run()
    ...
    target_cnt = len(NodeSet(target))
    self.assertEqual(teh.ev_start_cnt, 1)
    self.assertEqual(teh.ev_pickup_cnt, target_cnt)
    self.assertEqual(teh.ev_read_cnt, target_cnt)           # cat echoes back
    self.assertEqual(teh.ev_written_cnt, target_cnt)
    self.assertEqual(teh.ev_written_sz, target_cnt * len('Lorem Ipsum'))
    self.assertEqual(teh.ev_hup_cnt, target_cnt)
    self.assertEqual(teh.last_read, b'Lorem Ipsum')         # cat output
```

In plain words: run `cat` on the two distant targets through the two
gateways (one failing), write `"Lorem Ipsum"` to stdin, expect each
target to echo it back on stdout.

## What we observed in real clush

Session 2026-05-21 demo against the same gw2f1 topology, running
`clush --topology=... -w 127.0.0.[2-3] -b 'cat'` with `"input line"`
on stdin:

```
clush: enabling tree topology (2 gateways)
127.0.0.2: input line                              ← OK: working gateway
clush: 1/2 gw 1 write: ... B/s                     ← progress counter
192.0.2.0: ssh: connect to host 192.0.2.0 ... timeout
clush: rerouting commands for 127.0.0.3 ...
clush: 1/2 gw 1 write: 0 B/s ... (continues       ← progress STUCK on writes
        repeating "1/2 gw 1 write: 0 B/s")          targeting the rerouted
                                                    node, never reaching 2/2
```

The **working** target receives stdin and echoes back. The
**rerouted** target's stdin is lost; the progress display stays at
`1/2 gw 1 write: 0 B/s` indefinitely and `cat` on the rerouted node
never gets input.

## Likely root cause

`TreeWorker.write(buf)` → `TreeWorker._write_remote(buf)` (Tree.py:578):

```python
def _write_remote(self, buf):
    """Write buf to remote clients only."""
    for gateway, targets in self.gwtargets.items():
        assert len(targets) > 0
        self.task._pchannel(gateway, self).write(nodes=targets, buf=buf,
                                                 worker=self)
```

`PropagationChannel.write` (`Propagation.py:305`) queues a write CTL on
the channel and appends to `_cfg_write_hist`:

```python
def write(self, nodes, buf, worker):
    self.logger.debug("write buflen=%d", len(buf))
    assert id(worker) in self.workers

    ctl = ControlMessage(id(worker))
    ctl.action = 'write'
    ctl.target = nodes

    ctl_data = {
        'buf': buf,
    }
    ctl.data_encode(ctl_data)
    self._cfg_write_hist.appendleft((ctl.msgid, nodes, len(buf), worker))
    self.send_queued(ctl)
```

**Hypothesis**: when a gateway fails before `setup=True`:

1. The write CTL was queued in the failed channel's `_sendq` along with
   the original shell CTL.
2. The failed channel is destroyed; its `_sendq` and `_cfg_write_hist`
   are garbage-collected with it.
3. `_relaunch` (Tree.py:414) calls `_launch(targets)` which dispatches
   the targets to a working gateway and issues a **fresh** `shell()`
   CTL on the new channel.
4. **No mechanism re-queues the pending write data on the new
   channel.** The `_cfg_write_hist` from the failed channel is gone;
   `_relaunch` only handles the shell command, not user-supplied
   stdin.

Net effect: `cat` runs on the rerouted node but never receives the
buffered stdin → it sits idle, the user-side `ev_written_cnt` never
reaches its target, and the progress display hangs.

The `_write_remote` invariant assumes channels are stable for the
worker's lifetime, which is broken by reroute.

## Why does the MT (separate-thread) sibling work then?

Open question. A few guesses worth verifying:

1. **Timing**: in a separate task/thread, the engine may detect the
   failed gateway before the user-side `worker.write()` is processed,
   so by the time write enters `_write_remote`, `self.gwtargets` only
   contains the working gateway's targets. No write goes to the
   failed channel, no loss on reroute.
2. **Concurrency**: the separate-thread variant may serialize differently
   and end up routing the write CTL after `_launch()` for the rerouted
   targets has run, so the channel for the new gateway is already in
   `gwtargets`.
3. **The MT variant might also be subtly broken** but happen to pass
   most of the time due to the above timing. Worth running it 50+ times
   to see if it ever fails. The single-thread variant was disabled
   presumably because it was reliably failing.

## Code areas to investigate

| File / region | Why |
|---|---|
| `lib/ClusterShell/Worker/Tree.py:578-589` (`_write_remote`, `_set_write_eof_remote`) | The user-facing write path. Does not retain a buffer for replay. |
| `lib/ClusterShell/Worker/Tree.py:414-432` (`_relaunch`) | Only re-launches the shell command; never re-writes stdin. |
| `lib/ClusterShell/Propagation.py:305-329` (`PropagationChannel.write`, `set_write_eof`) | Where writes get queued. Note the existing `_cfg_write_hist` deque is per-channel and dies with the channel. |
| `lib/ClusterShell/Propagation.py:341-352` (`PropagationChannel.recv_ctl`, ACK path) | Where `_on_written` events fire. Dropped writes never produce an ACK; the meta worker's `ev_written_cnt` never increments for the rerouted node. |
| `lib/ClusterShell/Worker/Tree.py:592-624` (`write`, `set_write_eof`, pre-started buffering via `self._port.msg_send`) | The pre-started write buffer; one possible place to replay buffered writes after reroute. |

## Possible fix sketches (to discuss, NOT to implement here)

1. **Per-worker write buffer on the TreeWorker side**: TreeWorker
   remembers the writes it has issued and replays them on each reroute.
   Simple, but unbounded memory for large stdin payloads.

2. **Move `_cfg_write_hist` upstream**: track pending writes on the
   meta worker (TreeWorker) rather than per-channel, and on reroute,
   re-queue any writes that hadn't yet been ACKed on the failed channel.

3. **Refuse to support write+reroute as a documented limitation**:
   write `task.write()` blocks until at least one CTL has been
   ACKed on every active channel, so reroute can no longer happen
   silently after a write. Worse UX but simplest semantics.

4. **Replay only `_cfg_write_hist` entries that hadn't been ACKed**:
   pass them from the failed channel to the new one before destroying
   the failed channel.

## Reproduction recipe

Run on this VM (or any with the SSH-self-loopback setup from
`setup_env_root.sh`):

```bash
cd /home/sthiell/Documents/Claude/clustershell
source .venv/bin/activate
export CLUSTERSHELL_GW_PYTHON_EXECUTABLE=$(which python)

cat > /tmp/gw2f1.conf <<EOF
[routes]
$(hostname -s): 127.0.0.6,192.0.2.0
127.0.0.6,192.0.2.0: 127.0.0.[2-3]
EOF

echo "stdin payload" | timeout 30 clush \
    --topology=/tmp/gw2f1.conf \
    -O verbosity=2 \
    -w 127.0.0.[2-3] \
    -b 'cat'
```

Expected: both targets echo `"stdin payload"`. Actual: only the
working-gateway target echoes it; the rerouted target receives no
stdin; progress counter hangs at `1/2 gw 1 write: 0 B/s`.

For the unit-test variant, manually re-enable
`test_tree_run_gw2f1_write_distant2` in `tests/TreeWorkerTest.py` and
run it. With 5-min timeout it'll hang on `task.run()` and either
eventually fail an assertion or time out.

## Open questions for the future session

1. Does the `_mt` variant ALWAYS pass, or is it just flake-passing?
2. Is there a real-world clush use case that depends on stdin write
   through a failing gateway, or is this corner-case-only?
3. Does the same problem exist for `_set_write_eof_remote()`? Likely
   yes by symmetry.
4. Is there an existing upstream issue tracking this? (Search the
   ClusterShell repo's issues for "stdin", "write", "reroute".)
5. Worth filing as its own GitHub issue so the FIXME has a permanent
   home outside this private notes file.

---

**Status**: out of scope for PR #594. File a separate issue if and
when this work is picked up. The disabled test stays disabled.

**Investigator (initial)**: this session, 2026-05-21
**Codebase ref**: `master` at `c09f827` (after PR #594 work,
behaviour identical since none of this PR touches stdin write or
reroute-write-replay logic).

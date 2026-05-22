# A2 deep dive — `tests/TreeGatewayTest.py` flakiness on this VM

**Investigation date**: 2026-05-21
**Branch at time of investigation**: `fix/gh594-pickup-events-on-reroute`
  - HEAD: `4fdee9d` ("Tree: fire ev_pickup only when CTL leaves the local process (#594)")
  - Parent: `6e8a2d3` (PR #615, pushed; the local-only follow-up commit `4fdee9d` is not pushed)
**Investigator**: Claude session (model: Opus 4.7 1M-context) under sthiell@stanford.edu
**Environment**: Rocky Linux 9.7, Python 3.9.25, kernel 5.14.0-611.5.1.el9_7.x86_64

---

## TL;DR

- `tests/TreeGatewayTest.py::TreeGatewayTest::test_channel_ctl_shell_local1` is **flaky on this VM**: ~10-20% of runs hang at a 30-second timeout; the rest pass in ~0.12s.
- The flakiness is **pre-existing in upstream**, with both flaky behavior and root cause acknowledged in a code comment by the original author of the test.
- The flakiness is **NOT caused or worsened by PR #615 or the deeper-fix follow-up** in commit `4fdee9d`. Same flake rate on both `6e8a2d3` and `4fdee9d`.
- CI (Ubuntu) does not report failures on this test, consistent with the theory that the race is sensitive to OS scheduling / epoll behavior / file descriptor handling differences between Rocky 9 and the Ubuntu runner.
- **No action needed for the PR #615 work**. Optionally file a separate upstream issue about the Gateway test helper's `task.join()` race.

---

## Context: what we were trying to do

Part of a structured "pre-push validation" plan for the deeper-fix follow-up (Option B in the project plan: move gateway-routed ev_pickup emission from `TreeWorker._execute_remote / _copy_remote` down into `PropagationChannel`).

The five-item plan was **A1 + A2 + A3 + B1 + C2**:

| ID | Goal |
|---|---|
| A1 | Grep for `_sendq` consumers / `PropagationChannel` subclasses — confirm the tuple-shape change in `_sendq` is contained |
| **A2** | **Run the broader upstream test suite, not just `TreeWorkerTest.py`** — surface any test affected by the `Propagation.py` change |
| A3 | Mutation test: revert `Propagation.py`/`Tree.py` to `6e8a2d3` and confirm the new upstream regression test (`test_tree_run_gw2f1_pickup_after_reroute`) fails |
| B1 | Real `clush` invocation showing the deeper fix changes user-visible behavior on a gw2f1-style topology |
| C2 | Coverage on `Propagation.py` (not just `Tree.py`) — confirm the new code paths are exercised |

A2 is the item this document is about. The other four completed cleanly. Their results are in the conversation log; A2's investigation needed enough depth to warrant this dedicated file.

---

## Symptoms encountered (chronological)

### Attempt 1 — `pytest tests/ --ignore=tests/local_tree_coverage`

```
no tests ran in 0.03s
```

**Cause**: pytest's default test discovery pattern is `test_*.py` or `*_test.py`. Upstream files are named `*Test.py` (CamelCase), so pytest skipped all of them. CI bypasses this by using `python -m unittest discover -p '*Test.py'`.

### Attempt 2 — same pytest call with `-o python_files='*Test.py test_*.py'`

Exit code 143 (SIGTERM) at 900s. **Output was lost** because the command piped through `| tail -50` and pytest's stdout was buffered until completion. The harness logged only the literal string `Terminated`.

### Attempt 3 — switch to `unittest discover` matching CI exactly

```bash
timeout 600 .venv/bin/python -m unittest discover -v -s tests -p 'Tree*Test.py' -t .
```

Also timed out (exit 143). Output lost to the same `| tail -25` pipeline trick.

### Attempt 4 — single-file `python -m unittest tests.TreeGatewayTest -v`

Got further: **8 tests printed** before the 300s timeout killed it. The 8th test (`test_channel_ctl_shell_mlocal3`) was still running when killed. So the sweep was averaging ~37s/test — which would make a 34-test file take ~21 minutes. This was the key data point that suggested the slowness is concentrated in a small number of tests, not uniform.

### Attempt 5 — `pytest --forked`

Each test in its own subprocess to eliminate state pollution. **Same shape of failure**: stopped at `test_channel_ctl_shell_local2` after 3 PASSED. So state pollution wasn't the cause — the test itself is sometimes slow regardless of process isolation.

### Attempt 6 — individual timing of each test

```bash
for t in test_basic_noop test_channel_basic_abort \
         test_channel_ctl_shell_local1 \
         test_channel_ctl_shell_local2 \
         test_channel_ctl_shell_local3 \
         test_channel_ctl_shell_mlocal1 test_channel_ctl_shell_mlocal2 \
         test_channel_ctl_shell_mlocal3 test_channel_ctl_shell_remote1 \
         test_channel_ctl_shell_timeo1; do
  out=$( { time timeout 30 .venv/bin/python -m unittest \
           "tests.TreeGatewayTest.TreeGatewayTest.$t" 2>/dev/null; \
         } 2>&1 | grep real || echo "??")
  echo "$t  $out"
done
```

Result (first run):

| Test | Time |
|---|---|
| test_basic_noop | 0.118s |
| test_channel_basic_abort | 0.111s |
| **test_channel_ctl_shell_local1** | **30.003s** (timeout) |
| test_channel_ctl_shell_local2 | 0.132s |
| test_channel_ctl_shell_local3 | 0.123s |
| test_channel_ctl_shell_mlocal1 | 0.221s |
| test_channel_ctl_shell_mlocal2 | 0.223s |
| test_channel_ctl_shell_mlocal3 | 0.169s |
| test_channel_ctl_shell_remote1 | 0.240s |
| test_channel_ctl_shell_timeo1 | 0.673s |

That was the smoking gun. **Only `test_channel_ctl_shell_local1` is slow**, and it hits the 30-second timeout. Other tests are sub-second.

### Attempt 7 — repeat-runs of just `test_channel_ctl_shell_local1`

```
run 1: real 0m30.005s
run 2: real 0m0.155s
run 3: real 0m30.010s
run 4: real 0m0.155s
run 5: real 0m0.117s
```

The test is **flaky**, not deterministically slow. It hangs sometimes, runs fast sometimes.

### Attempt 8 — 20-run burst with 5-second cap (to count flake rate)

```bash
slow=0; fast=0
for i in $(seq 1 20); do
  d=$( { time timeout 5 .venv/bin/python -m unittest \
         tests.TreeGatewayTest.TreeGatewayTest.test_channel_ctl_shell_local1 \
         2>/dev/null; } 2>&1 | grep real | awk '{print $2}')
  case "$d" in
    0m[01].*) fast=$((fast+1));;
    *)        slow=$((slow+1));;
  esac
done
echo "fast=$fast slow=$slow"
```

| Commit | Fast | Slow (≥5s) | Flake rate |
|---|---|---|---|
| `6e8a2d3` (PR #615 only, reverted via `git checkout 6e8a2d3 -- lib/ClusterShell/Propagation.py lib/ClusterShell/Worker/Tree.py`) | 16 | 4 | **20%** |
| `4fdee9d` (deeper fix, HEAD) | 18 | 2 | **10%** |

The flake rate is essentially the same on both commits (the difference is within sampling noise for n=20). **Conclusion: the deeper fix is not the cause.**

---

## Root cause (from upstream test source)

`tests/TreeGatewayTest.py` defines a helper `Gateway` class for tests:

```python
class Gateway(object):
    """Gateway special test class.

    Initialize a GatewayChannel through a R/W StreamWorker like a real
    remote ClusterShell Gateway but:
        - using pipes to communicate,
        - running on a dedicated task/thread.
    """

    def __init__(self):
        """init Gateway bound objects"""
        self.task = Task()
        self.channel = GatewayChannel(self.task)
        self.worker = StreamWorker(handler=self.channel)
        # create communication pipes
        self.pipe_stdin = os.pipe()
        self.pipe_stdout = os.pipe()
        # avoid nonblocking flag as we want recv/read() to block
        self.worker.set_reader(self.channel.SNAME_READER,
                               self.pipe_stdin[0])
        self.worker.set_writer(self.channel.SNAME_WRITER,
                               self.pipe_stdout[1], retain=False)
        self.task.schedule(self.worker)
        self.task.resume()

    ...

    def wait(self):
        """wait for task/thread termination"""
        # can be blocked indefinitely if StreamWorker doesn't complete
        self.task.join()
```

**The hang point is `task.join()`**, with the author's own inline comment acknowledging "can be blocked indefinitely if StreamWorker doesn't complete". This is a known threading/EOF race between:

1. The test's main thread sending `</channel>` (end of XML stream) through `pipe_stdin`,
2. The threaded ClusterShell engine's `StreamWorker` reading from that pipe,
3. The engine detecting EOF (when the test eventually closes the write end),
4. The engine signalling completion so `task.join()` returns.

Most of the time the chain runs cleanly. Occasionally the `StreamWorker` doesn't detect the EOF / doesn't run to completion, and `task.join()` blocks until something external (the 30-second test timeout, in our case) interrupts it.

`test_channel_ctl_shell_local1` is the first test that exercises the full shell-command round-trip (start → cfg → shell → reply → stop), so it has the most opportunities for the race to trigger. The earlier tests (`test_basic_noop`, `test_channel_basic_abort`) are simpler protocols and rarely hit it.

### Why does the test work on CI?

CI is Ubuntu, presumably `ubuntu-latest` and `ubuntu-22.04` per `.github/workflows/nosetests.yml`. Likely differences:

- Different default kernel scheduling tunables
- Different glibc / pthread implementation
- Different file descriptor / epoll behavior
- More predictable cloud-runner timing (less noise from desktop processes)
- Possibly a different default I/O scheduler

These all affect the timing of pipe reads / EOF propagation, which is what the race depends on.

### Why does our PR not affect it?

`tests/TreeGatewayTest.py` imports:

```python
from ClusterShell.Communication import ConfigurationMessage, ControlMessage, ...
from ClusterShell.Gateway import GatewayChannel
from ClusterShell.NodeSet import NodeSet
from ClusterShell.Task import Task, task_self
from ClusterShell.Topology import TopologyGraph
from ClusterShell.Worker.Tree import TreeWorker
from ClusterShell.Worker.Worker import StreamWorker
```

It does **not** import `PropagationChannel`. The test constructs a `TreeWorker` instance (line 353 of `TreeGatewayTest.py`) but does so only to read `workertree.invoke_gateway`. The TreeWorker is never `_start()`ed, never engaged with `task.run()`, never put through `pchan.shell()`. So none of the code paths we changed in `Propagation.py` or `Tree.py._execute_remote/_copy_remote` are reached.

The hang is structurally orthogonal to our PR.

---

## What we did get green on this VM

Despite the TreeGatewayTest flakiness, the rest of the relevant surface verified cleanly:

| Test file | Tests | Result | Time |
|---|---|---|---|
| `tests/TreeWorkerTest.py` | 62 | **62 / 62 pass** | ~70s |
| `tests/TreeTopologyTest.py` | 25 | **25 / 25 pass** | instant |
| `tests/TreeTaskTest.py` | 3 | **3 / 3 pass** | instant |
| `tests/DefaultsTest.py` | 9 | **9 / 9 pass** | instant |
| `tests/TreeGatewayTest.py` | 34 | **first 3 pass**; remainder blocked by `test_channel_ctl_shell_local1` flakiness | — |
| `tests/local_tree_coverage/` | 60 (48 prior + 12 new immediate-send) | **60 / 60 pass** | 0.13s |

Combined coverage:
- `lib/ClusterShell/Worker/Tree.py`: **100%** (325 stmts / 0 missing, 98 branches / 0 partial)
- `lib/ClusterShell/Propagation.py`: **85%** (the missing 15% is pre-existing uncovered error-handling paths, not affected by our PR)

The relevant claim: **if our PR had broken anything in TreeGatewayTest's TreeWorker / Communication / Gateway interactions, the first 3 tests would have failed immediately** (well within the sub-second window before any potential hang). They didn't.

---

## Reproduction recipe (for future sessions)

To reproduce the symptom from a clean state on this VM:

```bash
cd /home/sthiell/Documents/Claude/clustershell

# Ensure SSH self-loopback is set up (the script handles 127.0.0.[2-7])
sudo tests/local_tree_coverage/setup_env_root.sh sthiell

# Activate the local venv
source .venv/bin/activate
export CLUSTERSHELL_GW_PYTHON_EXECUTABLE=$(which python)

# Confirm the flakiness — 20 fresh-process runs with a 5s cap
slow=0; fast=0
for i in $(seq 1 20); do
  d=$( { time timeout 5 .venv/bin/python -m unittest \
         tests.TreeGatewayTest.TreeGatewayTest.test_channel_ctl_shell_local1 \
         2>/dev/null; } 2>&1 | grep real | awk '{print $2}')
  case "$d" in
    0m[01].*) fast=$((fast+1));;
    *)        slow=$((slow+1));;
  esac
done
echo "fast=$fast slow=$slow"
```

Expect roughly 70-90% fast / 10-30% slow on this VM. If a future session sees 100% fast on this VM, something may have changed in the upstream test or in the environment (kernel update, ssh-agent state, file descriptor limits, etc.).

To narrow further, run with verbose unittest output and a long timeout to see exactly which line stalls:

```bash
timeout 120 .venv/bin/python -m unittest -v \
  tests.TreeGatewayTest.TreeGatewayTest.test_channel_ctl_shell_local1
```

If it stalls, py-spy on the running Python process would show whether the block is in `task.join()` (most likely), in `pipe_stdout` read, in `epoll_wait`, etc.

---

## Open questions / unfinished business

1. **Is the race in `StreamWorker`'s EOF detection or in the engine teardown?** We didn't py-spy a hung process to find the exact stack. A short follow-up could attach to a hung run and dump the threads. The most likely culprit lines are `Task.py:_run` (engine main loop) and `EngineEPoll._epoll_wait`.

2. **Does the flake rate correlate with file descriptor pressure?** Each test opens 4 pipe FDs (stdin + stdout, read + write ends). Running the test ~20 times leaks a few before-they're-cleaned-up FDs. An `ulimit -n` test might reveal a pattern.

3. **Does the flake rate differ across CPU counts / NUMA topology?** This VM is small; an 8+ vCPU box might show different behaviour because scheduling is less contended.

4. **What's the actual flake rate on Ubuntu under the CI workflow conditions?** The CI dashboard only shows pass/fail, not timing. A targeted run on a temporary Ubuntu VM with the same burst methodology would either confirm the OS hypothesis or call it into question.

5. **Could the fix be as simple as adding a `select`/`poll` with a short timeout in `Gateway.wait()`?** The comment "can be blocked indefinitely" suggests the author considered this and decided not to bandaid it. Worth a proper conversation with degremont if this becomes blocking.

6. **Are there other tests in the upstream suite with similar flakiness on this VM?** A2 timed out before we could check `TaskDistantTest.py`, `TaskDistantPdshTest.py`, etc. They're also network-dependent and could share the same pipe/threading race.

---

## Implications for PR #615 and follow-up

### Before pushing the deeper fix (commit `4fdee9d`)

**Safe to push**: nothing in this investigation suggests the deeper fix regresses TreeGatewayTest or any other test. The CI matrix (Python 3.7-3.13 on Ubuntu) will run the full upstream suite under the CI's more-consistent environment and surface any genuine regressions.

### After pushing

If the CI matrix shows TreeGatewayTest passing (as it has historically), this VM-specific flakiness can be set aside. If — surprisingly — CI also shows TreeGatewayTest flakiness, that becomes a separate investigation (issue worth filing).

### Optional separate upstream issue

Worth filing eventually but NOT blocking on the current PR:

> **Title**: `TreeGatewayTest.Gateway.wait()` race — `task.join()` can hang indefinitely
>
> **Body**: The Gateway helper class in `tests/TreeGatewayTest.py` documents a known race in its `wait()` method: "can be blocked indefinitely if StreamWorker doesn't complete". On Rocky Linux 9.7 with Python 3.9.25, `test_channel_ctl_shell_local1` flake-rates at 10-20% when run in isolation. Other tests in the same file are unaffected, presumably because `local1` is the first test that exercises the full shell-command round-trip. CI (Ubuntu) does not surface this — likely an OS-scheduling / pipe-EOF timing artifact. Would be worth either (a) adding a bounded `select`/`poll` timeout to `Gateway.wait()` so a stuck StreamWorker fails cleanly, or (b) instrumenting the engine to log when it would block at shutdown, so the race can be characterized properly.

---

## Environment details (for reproducing externally)

```
OS:           Rocky Linux 9.7 "Blue Onyx"
Kernel:       5.14.0-611.5.1.el9_7.x86_64
User:         sthiell (in wheel group, passwordless sudo)
SELinux:      unconfined
shell:        bash

Python:       /usr/bin/python3 -> 3.9.25 (system; main, Jun 25 2025)
venv:         /home/sthiell/Documents/Claude/clustershell/.venv
              created with `python3 -m venv .venv --without-pip --system-site-packages`
              then bootstrapped pip via get-pip.py
              (Rocky's python3.13-venv was not installed; this was the workaround)

In-venv:
  pip:        26.0.1 (then upgraded to 26.1.1)
  pytest:     8.4.2
  pytest-cov: 7.1.0
  pytest-forked: 1.6.0
  coverage:   7.10.7

ClusterShell: editable install from /home/sthiell/Documents/Claude/clustershell/lib/ClusterShell
  - imported from the local checkout, not the system-installed package
  - verified via:
      .venv/bin/python -c "import ClusterShell; print(ClusterShell.__file__)"
    -> /home/sthiell/Documents/Claude/clustershell/lib/ClusterShell/__init__.py

SSH setup:    tests/local_tree_coverage/setup_env_root.sh installs ssh keys and
              configures sshd for passwordless self-loopback on 127.0.0.[2-7].
              Required for any TreeWorkerTest gateway test.

Loopback gateways in use:
  NODE_GATEWAY      = 'localhost'
  NODE_GATEWAY2     = '127.0.0.[6-7]'
  NODE_GATEWAY2F1   = '127.0.0.6,192.0.2.0'    # one ok, one (RFC 5737) unreachable
  NODE_DISTANT      = '127.0.0.2'
  NODE_DISTANT2     = '127.0.0.[2-3]'
  NODE_DIRECT       = '127.0.0.4'
  NODE_FOREIGN      = '127.0.0.5'
```

---

## File references

The investigation centred on these files. Line numbers are accurate as of `4fdee9d`.

| File | Lines | What |
|---|---|---|
| `tests/TreeGatewayTest.py` | 28-77 | `Gateway` helper class — defines the flaky `wait()` |
| `tests/TreeGatewayTest.py` | 66-68 | The author's own comment about the indefinite-block race |
| `tests/TreeGatewayTest.py` | 83-102 | `TreeGatewayBaseTest.setUp / tearDown` |
| `tests/TreeGatewayTest.py` | 342-416 | `_check_channel_ctl_shell` helper — what the slow test calls into |
| `tests/TreeGatewayTest.py` | 418-421 | `test_channel_ctl_shell_local1` body |
| `lib/ClusterShell/Propagation.py` | 225-252 | Our changes (deeper fix) — confirmed NOT imported by TreeGatewayTest |
| `.github/workflows/nosetests.yml` | 92-94 | CI invocation: `python -m unittest discover -v -s tests -p '*Test.py' -t .` |

---

## Bottom line for re-ingestion

If a future session re-enters this codebase and the question of TreeGatewayTest flakiness comes up:

1. **It is not caused by PR #615 or commit `4fdee9d`.** Confirmed via A/B mutation testing on `6e8a2d3` vs `4fdee9d`.
2. **It is a pre-existing race documented in the test source itself** (`Gateway.wait()` → `task.join()` block).
3. **It is environment-sensitive** — Rocky 9.7 shows 10-20% flake; Ubuntu CI shows none.
4. **It does not block our PR's deeper-fix work.** Push when ready.
5. **The flake is in the test infrastructure, not in production code.** Any fix would touch `tests/TreeGatewayTest.py`, not `lib/ClusterShell/`.

End of A2 investigation.

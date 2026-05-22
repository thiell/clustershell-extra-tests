# clustershell-extra-tests

Supplementary unit and coverage tests for
[ClusterShell](https://github.com/clustershell/clustershell), focused
on hard-to-reach paths in `lib/ClusterShell/Worker/Tree.py` and the
propagation tree machinery that surrounds it.

> **Independent companion project.** Not affiliated with the upstream
> ClusterShell project, not part of the official test suite. Findings
> are reported upstream as they mature; see [`docs/findings.md`](docs/findings.md).

## Why this exists

ClusterShell's main test suite exercises the public surface very well
but the propagation tree (`Worker/Tree.py`, `Propagation.py`) has a
handful of private methods, error-path branches, and timing-sensitive
event flows that are not directly reachable from the public API
without spinning up real gateways and SSH. This project takes a
different angle:

- **No-engine fixtures.** A handful of `FakeTask` / `FakeRouter`
  fixtures (see [`tests/conftest.py`](tests/conftest.py)) let the
  tests instantiate a real `TreeWorker` and then call its private
  methods directly with controlled inputs.
- **Targeted at branch coverage.** Each test file is scoped to one
  method or a tightly related pair, so the test suite reads as a
  spec for "what should this method do in each branch". Combined with
  the upstream test suite, the two together drive `Worker/Tree.py`
  to 100% line and branch coverage; on its own this project's suite
  hits the branches upstream doesn't reach.
- **Surfaces latent bugs.** While driving Tree.py to full coverage
  we noticed several genuine bugs. They are catalogued in
  [`docs/findings.md`](docs/findings.md) and link back to the
  regression tests that pin them down.

## Quick start

```bash
git clone https://github.com/thiell/clustershell-extra-tests.git
cd clustershell-extra-tests

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

pytest
```

To run with coverage on `Worker/Tree.py`:

```bash
pytest --cov=ClusterShell.Worker.Tree --cov-report=term-missing
```

To run the integration tests (require SSH set up to 127.0.0.6 →
127.0.0.[2-3]; see [`scripts/setup_env_root.sh`](scripts/setup_env_root.sh)):

```bash
pytest -m integration
```

## Compatibility

The fixtures and tests target ClusterShell's tree-propagation code.
Specific commit dependencies:

| Test surface | Requires |
|---|---|
| Most unit tests under `tests/tree/` | ClusterShell with PR #594 (commit `9e688cc`), on upstream master but not yet in PyPI 1.9.3 |
| `tests/tree/test_extractall_filter.py` | ClusterShell with the PEP 706 tarfile fix (on `thiell/clustershell@fix/tree-extractall-pep706`); auto-skipped on installs that lack `Tree._TAR_EXTRACT_KWARGS` |
| Tests marked `@pytest.mark.integration` | Live SSH + a gw2f1 propagation topology; excluded from default `pytest` runs |

When ClusterShell ships a release containing PR #594, the
`requirements.txt` pin will move back to `ClusterShell>=1.9.4` and
the master-only quick-start step goes away.

## What's tested today

All tests live under [`tests/tree/`](tests/tree/). One sentence per file:

| File | Pins |
|------|------|
| `test_check_ini_unit.py` | `_check_ini` event-flush sequencing on first child start |
| `test_check_fini_unit.py` | `_check_fini` event-emission ordering at worker close |
| `test_emit_pickup_at_most_once.py` | the no-double-pickup invariant under reroute (mutation-testable, see findings.md) |
| `test_emit_pickup_unit.py` | direct-call coverage of `_emit_pickup` branches |
| `test_extractall_filter.py` | PEP 706: rcopy passes `filter='tar'` on supporting Pythons, omits the kwarg on legacy |
| `test_gateway_abort.py` | `_gateway_abort` reroute and final-failure paths |
| `test_init_topology.py` | TreeWorker `__init__` topology-resolution branches |
| `test_launch_branches.py` | `_launch` for remote/local, with/without source |
| `test_metaworker_handler.py` | `MetaWorkerEventHandler` event-forwarding |
| `test_pickup_timing.py` | pickup-event timing relative to ev_start / abort |
| `test_propagation_immediate_send.py` | `PropagationChannel._send_ctl` immediate-send vs. queueing |
| `test_relaunch.py` | reroute-driven relaunch on a fresh gateway |
| `test_remote_close_rcopy.py` | `_on_remote_node_close` rcopy tar-extract path |
| `test_smoke.py` | sanity check that the fixtures themselves work |
| `test_write_buffering.py` | `write()` aggregation across children |

The deeper story for each test is in the docstring at the top of the file.

## Findings

Issues observed while writing the tests, with file/line references,
severity, and suggested fixes:

- See [`docs/findings.md`](docs/findings.md).

Known flaky upstream tests on certain platforms (Rocky 9 in our case)
are catalogued separately in [`docs/flakiness/`](docs/flakiness/).

## How the fixtures work

The load-bearing trick is in [`tests/conftest.py`](tests/conftest.py):

```python
worker = TreeWorker(NodeSet(nodes), handler, timeout, **kwargs)
# Overwrite engine-dependent attributes AFTER __init__ ran.
worker.task = fake_task
worker.router = fake_router
```

The `TreeWorker.__init__` constructor runs its normal code path
(which we want covered), then the test reaches in and swaps out
`task` and `router` for hand-rolled stand-ins. After that, individual
tests can call private methods (`_emit_pickup`, `_check_ini`,
`_check_fini`, `_gateway_abort`, `abort`, ...) with controlled
inputs and assert on observable state, without ever starting the
event engine.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

This is a small project. The bar for new tests is "covers a branch
or pins behavior that today's upstream test suite doesn't". New
findings in `docs/findings.md` should include file path, line range,
severity, and a regression-test pointer.

## License

LGPL-2.1-or-later. See [`LICENSE`](LICENSE).

The license matches the upstream ClusterShell project so that any
test code donated upstream can be merged without a license change.

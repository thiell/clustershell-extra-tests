# Contributing

Thanks for your interest. This project is a focused set of unit and
coverage tests for [ClusterShell](https://github.com/clustershell/clustershell),
intentionally scoped so that each test pins a specific branch or
behavior. Contributions are welcome; please read the conventions
below before opening a PR.

## What belongs here

- **New tests** that exercise hard-to-reach code paths in ClusterShell,
  preferably in `Worker/Tree.py`, `Propagation.py`, or related
  modules. If a test can reasonably live in the upstream test suite,
  consider sending it there first.
- **New findings** added to `docs/findings.md` when a test reveals
  a latent bug. Each finding should include:
  - file path + line range (referenced against an explicit commit SHA),
  - a short observation,
  - severity assessment,
  - suggested fix,
  - a status line ("not reported", "PR #X", "fixed in vY.Z").
- **Fixture improvements** to `tests/conftest.py`, kept minimal.
  The fixtures intentionally stand in for only the surface area
  Tree.py actually touches.

## What doesn't belong here

- Integration tests that need a running gateway, real SSH, or root
  privileges (with the exception of helper scripts in `scripts/`).
- Tests duplicating coverage already provided by upstream
  `tests/TreeWorkerTest.py` or similar.
- Patches to ClusterShell itself — send those upstream.

## Code conventions

- Each test file gets a top-of-file docstring explaining what it
  pins and why.
- Code comments: one line max, only where the "why" is non-obvious.
  Don't restate what the code does.
- Imports: prefer the public `ClusterShell.*` API when feasible.
  When a test needs a private helper, import it explicitly so the
  dependency is visible.

## Running tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

For coverage on Tree.py:

```bash
pytest --cov=ClusterShell.Worker.Tree --cov-report=term-missing
```

For the canary build against upstream master:

```bash
pip install --upgrade \
    "ClusterShell @ git+https://github.com/clustershell/clustershell.git@master"
pytest
```

## Commit messages

Follow the style of the ClusterShell upstream repo: short imperative
subject line, optional body wrapped at ~72 cols, `Signed-off-by:` if
the change might eventually be donated upstream.

## License

By contributing, you agree your work will be licensed under
LGPL-2.1-or-later (see [`LICENSE`](LICENSE)).

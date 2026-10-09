# Contributing to Cliffracer

We welcome contributions to Cliffracer.

Cliffracer is an opinionated Python microservices framework built on NATS. The core framework focuses strictly on reliable RPC, event routing, and operational safety. Optional features (HTTP endpoints, metrics, authentication, OpenTelemetry, Key-Value stores, resilience) are packaged as separate extensions under `packages/`.

Before submitting changes, review this guide to understand our architectural principles, development workflow, and testing standards.

## Architectural Principles

1. **Explicit over Permissive**: Ambiguity fails fast at startup rather than producing surprising runtime behavior. Handlers must have explicit types and delivery semantics (such as declaring `fanout=True` or a durable consumer).
2. **Minimal Boring Core**: Core has minimal dependencies (`nats-py`, `pydantic`, `loguru`). Protocol extensions and additional capabilities belong in separate workspace packages.
3. **Mechanical Documentation**: Docstrings describe what the code does in concise, present-tense terms without emojis or historical commentary.

## Development Setup

Cliffracer requires Python 3.12 or higher and uses `uv` for workspace management.

### 1. Clone and Install Dependencies

```bash
git clone https://github.com/sndwch/cliffracer.git
cd cliffracer

# Install core and all workspace extensions in editable mode
uv sync --all-packages --extra dev
```

If you prefer standard `pip`:

```bash
pip install -e ".[dev]"
```

### 2. Local Broker Setup

Integration tests require a running NATS broker with JetStream enabled:

```bash
docker run -d --name cliffracer-nats \
  -p 4222:4222 -p 8222:8222 \
  nats:alpine -js -m 8222
```

A broker is used only when you name it. Without `CLIFFRACER_TEST_NATS_URL` the suite dials
nothing, and the tests marked `nats_required` (every test of `tests/integration/` carries it)
skip with the reason printed. To run them against the broker above:

```bash
CLIFFRACER_TEST_NATS_URL=nats://localhost:4222 uv run pytest
```

Name a broker of your own, not one other people use: the tests create and delete streams
and buckets on it (under a prefix of their own, swept at the end of the run).

A run that is killed, or times out, never reaches that sweep, and its streams stay on the broker.
`scripts/sweep_orphan_test_prefixes.py` removes them. It is run by hand and only reports until it is
given `--apply`:

```bash
uv run python scripts/sweep_orphan_test_prefixes.py --url nats://localhost:4222 --older-than 6
uv run python scripts/sweep_orphan_test_prefixes.py --url nats://localhost:4222 --older-than 6 --apply
```

The report names every stream and bucket it would delete, with its age, and every one it is leaving
alone. It deletes only a name shaped like a test session's prefix (`t`, hex digits, a worker token,
`_`) that is older than the threshold; a stream with any other name is never touched, and neither is
one whose age the server did not report. `--apply` refuses to run without an address named by `--url`
or `CLIFFRACER_TEST_NATS_URL`. Read the dry run first: the script decides by name, and a service's own
stream named like a test prefix would look the same.

## Quality and Style Standards

### The three gates CI runs

CI runs these three commands before it runs a single test. Run them over the
same paths CI uses, because a narrower path list passes while CI fails:

```bash
uv run ruff check src/ packages/ tests/ examples/ load-testing/ scripts/
uv run ruff format --check src/ packages/ tests/ examples/ load-testing/ scripts/
uv run mypy src/ packages/*/src
```

A green `pytest` says nothing about any of them. They run as separate steps, so
a formatting slip fails the build with no test having run. Read each of the
three before pushing rather than inferring them from the suite.

### Complexity Ceiling

Classes are subject to an AST complexity ceiling, enforced by automated tests. The ceiling is a ratchet: it is held close to the largest class in the tree, so growth in the biggest classes is what fails the build. `tests/repo/test_class_complexity_invariants.py` carries the value and the headroom it is allowed.

There is no per-class exemption. A class that has to exceed the ceiling is a reason to raise `CLASS_STATEMENT_CEILING` in that file, in the same change, with the reason in the commit. The failure message reports the largest class measured, which is the number to raise it above.

### Documentation Rules

Repository tests enforce that documentation contains:
- No emoji characters (`test_docs_carry_no_emoji.py`).
- No historical narrative, past bug references, or commit SHAs (`test_docs_carry_no_history.py`).
- Canonical project URLs (`test_no_stale_project_urls.py`).

## Testing Standards

All pull requests must pass the complete test suite:

```bash
# Run everything
uv run pytest

# The library, in process
uv run pytest tests/unit/

# The library over an in-memory transport, no broker
uv run pytest tests/transport/

# Against a live broker (set CLIFFRACER_TEST_NATS_URL, or these skip)
uv run pytest tests/integration/

# Guards over the repository: docs, packaging, CI, the suite
uv run pytest tests/repo/

# One extension
uv run pytest packages/cliffracer-kv/tests/
```

### Where a test goes

A test lives in the directory that matches what it exercises, and declares that
directory's tier once, at module level:

```python
import pytest

pytestmark = pytest.mark.unit
```

`docs/ARCHITECTURE.md` lists the directories and their tiers. `nats_required`
and `slow` are separate flags and go on the individual tests that need them.

### Naming

- **A file names its subject**: `test_health_listener.py`, not
  `test_the_health_listener_reports_503.py`. The functions inside it name the
  behaviour, which is where a sentence belongs.
- **Under `tests/repo/`, a file names the one invariant it enforces**:
  `test_docs_carry_no_emoji.py` says exactly what it guards, where
  `test_docs_emoji.py` would not.
- **A filename does not repeat its directory**: no `_integration` suffix inside
  `tests/integration/`.
- **A qualifier goes at the end**: `test_rpc_concurrency_adversarial.py`, so
  names sort and glob by subject. `adversarial` and `stress` are the qualifiers
  in use.
- Do not name tests after pull requests, issue numbers, milestones, or author
  handles.
- Add regression tests alongside any bug fix.

`tests/repo/test_the_suite_follows_its_conventions.py` enforces the tier rule
and the two mechanical filename rules.

### Prove the test can fail

A check that cannot go red is worse than no check, because it manufactures the
appearance of a gate. Where a test sweeps the tree, parses source, or asserts
that something is absent, pair it with a `test_CONTROL_` case that feeds the
checker an input it must reject, and another that feeds it one it must accept:

```python
def test_CONTROL_the_detector_catches_an_emoji():
    assert emoji_lines([fixture_with_an_emoji])


def test_CONTROL_typography_and_prose_are_not_flagged():
    assert not emoji_lines([fixture_with_only_prose])
```

The prefix is uppercase so these read as a distinct family in a run's output.

### Commit messages

A commit message says what the code does now. `scripts/check_commit_messages.py`
runs on every pull request and rejects a message carrying an issue or pull
request number, a commit hash, or narration about how the code came to be this
way. It shares its pattern set with the documentation guard, so the two agree
by construction.

`Closes #n` belongs in the pull request body, where it is read once and where
it does the linking. The same sentence in a commit message stays in the log
forever and fails this check.

### Running the benchmark job

The benchmark job compares against a recorded baseline and fails a metric that
moves more than 15%, so it measures a host with nothing else on it. It runs on
`workflow_dispatch` and on a push to `main`, and it is ordered after the test
job so the two never share the machine.

**That ordering separates the benchmark from other CI jobs, and nothing else.**
It does not constrain anyone running the suite locally on the same host, which
on the internal runner is the normal working state. A quiet host is a separate
operational requirement, so if you are about to dispatch the benchmark, say so
and let the host go quiet first.

The gate enforces the part it can see: it reads the one-minute load and, when
the run was taken at more than twice the load the baseline records, refuses to
score rather than reporting a comparison. That refusal fails the job by name --
`NOT SCORED: host 1-min load ...` -- so a release still does not go out on an
unmeasured build, and the log says to re-run on a quiet host rather than to go
looking through the diff.

It exists only in the Gitea workflow, on a runner labelled for the job. The
baseline is one machine's numbers, so a comparison against it means something
only on a host of that class; GitHub's hosted runners are shared and vary, so
nothing there scores a measurement.

A pull request runs the test job alone, and no test job scores a benchmark: the
test gate carries `-m "not benchmark"`, so the tier is not collected there at
all. It runs in the `benchmark` job on `bench-tier`, after the scored
measurement. To get a measurement for a branch, dispatch the Gitea workflow
against it.

## Pull Request Workflow

1. Fork the repository and create a branch from `main`.
2. Implement your fix or feature with accompanying tests and documentation.
3. Ensure formatting, type checks, and tests pass locally:
   ```bash
   uv run ruff format src/
   uv run ruff check src/
   uv run mypy src/
   uv run pytest
   ```
4. Open a Pull Request targeting `main`.
5. Ensure all automated CI checks pass.

### Reading a pull request

Review the change from its merge base rather than from the tip of `main`:

```bash
git diff "$(git merge-base main HEAD)" HEAD
```

A branch that predates a merge shows that merge's changes as deletions, so a
diff against the moving tip attributes other people's work to the author under
review. The merge base is what the three-way merge applies, and it is the only
left-hand side that answers "what did this author change".

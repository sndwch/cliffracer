# Contributing to Cliffracer

We welcome contributions to Cliffracer.

Cliffracer is an opinionated Python microservices framework built on NATS. The core framework focuses strictly on reliable RPC, event routing, and operational safety. Optional features (HTTP endpoints, metrics, authentication, OpenTelemetry, Key-Value stores, resilience) are packaged as separate extensions under `packages/`.

Before submitting changes, review this guide to understand our architectural principles, development workflow, and testing standards.

## Architectural Principles

1. **Explicit over Permissive**: Ambiguity fails fast at startup rather than producing surprising runtime behavior. Handlers must have explicit types and delivery semantics (such as declaring `fanout=True` or a durable consumer).
2. **Minimal Boring Core**: Core has minimal dependencies (`nats-py`, `pydantic`, `loguru`). Protocol extensions and additional capabilities belong in separate workspace packages.
3. **Mechanical Documentation**: Docstrings describe what the code does in concise, present-tense terms without emojis or historical commentary.

## Development Setup

Cliffracer requires Python 3.11 or higher and uses `uv` for workspace management.

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

## Quality and Style Standards

### Code Formatting and Linting

We enforce strict formatting and linting via Ruff:

```bash
uv run ruff check src/ packages/*/src/ tests/
uv run ruff format --check src/ packages/*/src/ tests/
```

### Static Type Checking

All core code and extensions must pass strict Mypy checks:

```bash
uv run mypy src/ packages/*/src/
```

### Complexity Ceiling

Classes are subject to an AST complexity ceiling of 500 statements, enforced by automated tests. When a class must exceed this ceiling, annotate it with `@override_length_check(reason="...")`.

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

# Against a live broker
uv run pytest tests/integration/

# Guards over the repository: docs, packaging, CI, the suite
uv run pytest tests/repo/

# One extension
uv run pytest packages/cliffracer-http/tests/
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

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
# Run all unit and integration tests
uv run pytest

# Run unit tests only
uv run pytest tests/unit/

# Run integration tests against a live broker
uv run pytest tests/integration/

# Run tests for a specific extension
uv run pytest packages/cliffracer-http/tests/
```

### Writing Tests

- Test names describe the architectural contract or behavior verified (e.g., `test_first_connect_is_bounded`, `test_duplicate_durables`).
- Do not name tests after pull requests, issue numbers, milestones, or author handles.
- Add regression tests alongside any bug fix.

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

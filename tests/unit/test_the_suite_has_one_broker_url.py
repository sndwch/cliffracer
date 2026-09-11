"""Verify a single environment variable configures the test suite's broker URL.

`$CLIFFRACER_TEST_NATS_URL` sets `ServiceConfig`'s default for the test process,
so the probe, skip reasons, fixtures, integration modules, and example
subprocesses all dial one address.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cliffracer import ServiceConfig
from tests.conftest import (
    DEFAULT_BROKER_URL,
    TEST_BROKER_URL_ENV,
    _apply_broker_url,
    broker_url,
    configured_broker_url,
)

REPO = Path(__file__).resolve().parents[2]

# Port 1 is privileged, so nothing of ours can be listening there by accident.
DEAD_URL = "nats://127.0.0.1:1"


@pytest.mark.unit
def test_the_suite_reports_the_address_it_will_dial():
    """`broker_url()` and what a service actually gets are the same string.

    They are the same expression today. Asserting it is what stops them
    drifting into a probe that reports one address while the tests use another
    -- the failure the old `$NATS_URL` produced.
    """
    assert broker_url() == ServiceConfig(name="probe").nats_url


@pytest.mark.unit
def test_the_variable_moves_the_default_and_an_explicit_url_still_wins():
    original = broker_url()
    try:
        _apply_broker_url("nats://localhost:4999")
        assert broker_url() == "nats://localhost:4999"
        assert ServiceConfig(name="a").nats_url == "nats://localhost:4999"
        assert ServiceConfig(name="b", nats_url="nats://elsewhere:1").nats_url == (
            "nats://elsewhere:1"
        )
    finally:
        _apply_broker_url(original)
    assert broker_url() == original


@pytest.mark.unit
def test_an_unset_variable_asks_for_nothing(monkeypatch):
    monkeypatch.delenv(TEST_BROKER_URL_ENV, raising=False)
    assert configured_broker_url() is None
    monkeypatch.setenv(TEST_BROKER_URL_ENV, "")
    assert configured_broker_url() is None, "an empty string is not an address"
    monkeypatch.setenv(TEST_BROKER_URL_ENV, "nats://somewhere:4222")
    assert configured_broker_url() == "nats://somewhere:4222"


def _collect_only(env_extra: dict) -> subprocess.CompletedProcess:
    """Run the real conftest through a collect-only pytest, in a subprocess.

    Collect-only because the rule under test fires in pytest_configure, before
    any test runs, so this costs a second rather than three minutes.
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_service_config.py",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO,
        env={**os.environ, **env_extra},
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
def test_an_explicit_broker_that_is_not_listening_fails_the_run():
    """Fails CLOSED on an address someone named.

    Skipping fifty tests because a named broker is absent is the outcome this
    whole change exists to remove: a green run that tested none of them. The
    same rule the health listener already follows -- an explicit port fails,
    the default one shrugs.
    """
    result = _collect_only({TEST_BROKER_URL_ENV: DEAD_URL})
    assert result.returncode != 0, result.stdout[-2000:]
    assert "names a broker that is not listening" in result.stdout + result.stderr


@pytest.mark.unit
def test_CONTROL_the_same_command_succeeds_without_the_variable():
    """The other half: the failure above is caused by the variable, not by the
    command. Without it, an absent broker is still only a skip."""
    result = _collect_only({})
    assert result.returncode == 0, result.stdout[-2000:]


@pytest.mark.unit
def test_the_default_is_unchanged_when_nobody_asks():
    """A refactor that quietly moved the default would break every consumer
    reading the documented `nats://localhost:4222`."""
    assert DEFAULT_BROKER_URL == "nats://localhost:4222"


# --- the rule, checked over the tree ------------------------------------------
#
# Check that integration test modules do not hardcode local broker addresses.

INTEGRATION = REPO / "tests" / "integration"

_LITERALS = ("nats://localhost", "nats://127.0.0.1", "localhost:4222", "127.0.0.1:4222")
_BROKER_PORT = 4222


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """`id()` of every node that is a docstring, so prose may name an address."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def pinned_broker_addresses() -> list[str]:
    """Every place under tests/integration that names a broker instead of asking."""
    found = []
    for path in sorted(INTEGRATION.rglob("*.py")):
        tree = ast.parse(path.read_text())
        skip = _docstring_nodes(tree)
        rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and id(node) not in skip:
                if isinstance(node.value, str) and any(lit in node.value for lit in _LITERALS):
                    found.append(f"{rel}:{node.lineno} literal {node.value!r}")
                # `True` is an int in Python, hence the exact type check.
                elif type(node.value) is int and node.value == _BROKER_PORT:
                    found.append(f"{rel}:{node.lineno} literal port {_BROKER_PORT}")
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None)
                if name in {"get", "getenv"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and first.value == "NATS_URL":
                        found.append(f"{rel}:{node.lineno} reads $NATS_URL, which nothing sets")
    return found


@pytest.mark.unit
def test_no_integration_module_pins_a_broker_address():
    pinned = pinned_broker_addresses()
    assert not pinned, (
        "these dial an address of their own instead of the suite's:\n  "
        + "\n  ".join(pinned)
        + f"\n\nDrop the URL and let `ServiceConfig`'s default apply; "
        f"${TEST_BROKER_URL_ENV} moves it. For a raw `nats.connect`, read "
        "`ServiceConfig.model_fields['nats_url'].default` at call time."
    )


@pytest.mark.unit
def test_CONTROL_the_scan_reads_real_files():
    """An empty file list would make the guard above green forever."""
    files = list(INTEGRATION.rglob("*.py"))
    assert len(files) > 5, files


@pytest.mark.unit
def test_CONTROL_a_pinned_address_is_detected(tmp_path, monkeypatch):
    """Both shapes, since the four modules split between them."""
    module = tmp_path / "test_planted.py"
    module.write_text(
        '"""nats://localhost:4222 in a docstring is prose, not a dial."""\n'
        "import os\n"
        'URL = "nats://localhost:4222"\n'
        'OTHER = os.environ.get("NATS_URL", "x")\n'
        'PAIR = ("localhost", 4222)\n'
    )
    monkeypatch.setattr(sys.modules[__name__], "INTEGRATION", tmp_path)
    found = pinned_broker_addresses()
    assert len(found) == 3, found
    assert any("literal 'nats://" in f for f in found), found
    assert any("$NATS_URL" in f for f in found), found
    assert any("literal port 4222" in f for f in found), found
    assert not any("docstring" in f for f in found), found

"""Verify a single environment variable configures the test suite's broker URL.

`$CLIFFRACER_TEST_NATS_URL` sets `ServiceConfig`'s default for the test process,
so the probe, skip reasons, fixtures, integration modules, and example
subprocesses all dial one address.

The sweep below covers every Python file under tests/, packages/*/tests, examples
and scripts. Files that pin an address today are named one by one in ALLOWED
with the number of hits each still has, so a new pin anywhere reddens the guard
and the standing debt cannot quietly grow.
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

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# Port 1 is privileged, so nothing of ours can be listening there by accident.
DEAD_URL = "nats://127.0.0.1:1"


def test_the_suite_reports_the_address_it_will_dial(monkeypatch):
    """Verify broker_url() and ServiceConfig match even when NATS_URL is set.

    Asserting parity stops them from drifting into a probe that reports
    one address while the tests use another. Setting ambient NATS_URL
    must not cause broker_url() and ServiceConfig to diverge.
    """
    assert broker_url() == ServiceConfig(name="probe").nats_url

    divergent_url = "nats://divergent-host:4222"
    monkeypatch.setenv("NATS_URL", divergent_url)
    assert broker_url() == ServiceConfig(name="probe").nats_url


def test_ambient_nats_url_warns_and_does_not_alter_dialed_address():
    """Ambient NATS_URL emits a warning and does not alter the dialed broker."""
    divergent_url = "nats://divergent-host:4999"
    result = _collect_only({"NATS_URL": divergent_url})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "does not move this suite" in result.stdout
    assert f"this run dials {DEFAULT_BROKER_URL}" in result.stdout


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


def test_an_unset_variable_asks_for_nothing(monkeypatch):
    monkeypatch.delenv(TEST_BROKER_URL_ENV, raising=False)
    assert configured_broker_url() is None
    monkeypatch.setenv(TEST_BROKER_URL_ENV, "")
    assert configured_broker_url() is None, "an empty string is not an address"
    monkeypatch.setenv(TEST_BROKER_URL_ENV, "nats://somewhere:4222")
    assert configured_broker_url() == "nats://somewhere:4222"


def _collect_only(
    env_extra: dict, extra_args: list[str] | None = None
) -> subprocess.CompletedProcess:
    """Run conftest through a collect-only pytest in an isolated subprocess.

    Collect-only executes pytest_configure without running test functions.
    Ensures ambient TEST_BROKER_URL_ENV is not inherited unless explicitly supplied.
    """
    clean_env = {k: v for k, v in os.environ.items() if k != TEST_BROKER_URL_ENV}
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/unit/test_service_config.py",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd,
        cwd=REPO,
        env={**clean_env, **env_extra},
        capture_output=True,
        text=True,
    )


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


def test_CONTROL_the_same_command_succeeds_without_the_variable(tmp_path: Path):
    """Verify that without the variable the absence of a broker is not a hard error.

    The failure in test_an_explicit_broker_that_is_not_listening_fails_the_run is
    caused by the variable demanding a specific broker. Without it, collection
    completes: the command exits 0, the probe reports that no broker was named and
    which address it did not dial, and the fail-closed message is absent. The child
    runs --collect-only, so no test is skipped here and none is observed to be.
    """
    # Point default ServiceConfig nats_url to a non-listening address via a temporary plugin
    plugin_file = tmp_path / "dead_default_broker.py"
    plugin_file.write_text(
        "from cliffracer.core.service_config import ServiceConfig\n"
        "ServiceConfig.model_fields['nats_url'].default = 'nats://127.0.0.1:1'\n"
        "ServiceConfig.model_rebuild(force=True)\n"
    )
    pythonpath = str(tmp_path)
    if "PYTHONPATH" in os.environ and os.environ["PYTHONPATH"]:
        pythonpath = f"{tmp_path}{os.pathsep}{os.environ['PYTHONPATH']}"

    result = _collect_only(
        env_extra={"PYTHONPATH": pythonpath},
        extra_args=["-p", "dead_default_broker"],
    )
    assert result.returncode == 0, (
        f"Expected returncode 0, got {result.returncode}:\n{result.stdout}"
    )
    assert "NO BROKER NAMED, not dialling nats://127.0.0.1:1" in result.stdout
    assert "names a broker that is not listening" not in (result.stdout + result.stderr)


def test_the_default_is_unchanged_when_nobody_asks():
    """Ensure documented default broker URL remains stable."""
    assert DEFAULT_BROKER_URL == "nats://localhost:4222"


# --- Tree-wide sweep covering integration, fixtures, examples, and scripts -----

SWEEP_DIRECTORIES = [
    REPO / "tests",
    REPO / "examples",
    REPO / "scripts",
    *(REPO / "packages").glob("*/tests"),
]

# Files that still dial an address of their own, each with the number of hits it
# carries. The count is exact on purpose: raising it needs a deliberate edit, and
# clearing one means lowering it, so the debt can only be paid down.
#
# This guard's own file holds the literals it searches for, which is why it is
# here rather than fixed.
ALLOWED: dict[str, tuple[int, str]] = {
    "packages/cliffracer-metrics/tests/test_pool_extension.py": (
        1,
        "Carries the broker port as a bare integer rather than taking the suite's address.",
    ),
    "tests/benchmark/benchmarks.py": (
        2,
        "Benchmarks dial a literal address and carry the broker port as a bare integer.",
    ),
    "tests/fixtures/secured_broker.py": (
        1,
        "The disposable authenticated broker's internal listening port is fixed; "
        "Docker allocates an ephemeral loopback host port, and every connection "
        "uses that allocated address. Authorization tests require their own user configuration.",
    ),
    "tests/repo/test_kv_compatibility_is_a_gate.py": (
        5,
        "Fake Docker port mappings and mocked probe/subprocess calls assert "
        "the requested address is forwarded. These literals are test data "
        "and are never dialed; real compatibility tests use the allocated broker URL.",
    ),
    "scripts/check_message_schedules.py": (
        2,
        "The message-schedule gate's own broker: it binds 127.0.0.1 port 0 to find a loopback "
        "port free, maps its container to it fixed so the contract can restart it, and dials "
        "that address before handing it to the suite as $CLIFFRACER_TEST_NATS_URL.",
    ),
    "tests/repo/test_message_schedules_is_a_gate.py": (
        1,
        "A fake Docker port mapping's address, asserted to reach the mocked probe and "
        "subprocess. Test data, never dialled; the live rows use the allocated broker URL.",
    ),
    "tests/repo/test_the_suite_has_one_broker_url.py": (
        13,
        "This guard holds the literals it searches for: the detector's own pattern "
        "list, the planted sample its control scans, and the dead and divergent "
        "addresses its subprocess cases dial on purpose.",
    ),
    "tests/unit/test_first_connect_bounds.py": (
        1,
        "Dials port 1 deliberately, which is privileged and never listening, to "
        "force a first-connect failure.",
    ),
    "tests/unit/test_generate_client_cli.py": (
        5,
        "Mixes argv fixtures carrying an address, a deliberately dead port 1, and "
        "two generated-client URLs.",
    ),
    "tests/unit/test_handler_discovery.py": (
        1,
        "Dials port 1 deliberately to force a connect failure during discovery.",
    ),
    "tests/unit/test_initial_connect_failure.py": (
        1,
        "Dials port 14222 deliberately, which nothing binds, to force an initial-connect failure.",
    ),
    "tests/unit/test_nats_auth.py": (
        4,
        "Asserts on credential handling and redaction, so the addresses are the data under test.",
    ),
    "tests/unit/test_service_config_unknown_fields.py": (
        1,
        "Passes a literal address to a misspelled field name, which is the point of the case.",
    ),
}

_LITERALS = ("nats://localhost", "nats://127.0.0.1", "localhost:4222", "127.0.0.1:4222")
_BROKER_PORT = 4222


def _discover_target_files() -> list[Path]:
    """Discover every test, fixture, example and script the rule covers."""
    files: set[Path] = set()
    for d in SWEEP_DIRECTORIES:
        if d.exists():
            files.update(d.rglob("*.py"))
    return sorted(files)


def _rel(path: Path) -> str:
    return (path.relative_to(REPO) if path.is_relative_to(REPO) else path).as_posix()


def _hits_by_file() -> dict[str, int]:
    """Count pinned-address hits per swept file."""
    counts: dict[str, int] = {}
    for path in _discover_target_files():
        hits = pinned_broker_addresses([path])
        if hits:
            counts[_rel(path)] = len(hits)
    return counts


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Extract id() of docstring nodes so prose may describe default addresses."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _eval_str_node(node: ast.AST) -> str | None:
    """Evaluate constant strings and constant string binary additions."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _eval_str_node(node.left)
        right = _eval_str_node(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def pinned_broker_addresses(files: list[Path] | None = None) -> list[str]:
    """Locate any code that hardcodes a broker address rather than asking."""
    target_files = files if files is not None else _discover_target_files()
    found = []
    for path in target_files:
        tree = ast.parse(path.read_text())
        skip = _docstring_nodes(tree)
        rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and id(node) not in skip:
                if isinstance(node.value, str) and any(lit in node.value for lit in _LITERALS):
                    found.append(f"{rel}:{node.lineno} literal {node.value!r}")
                elif type(node.value) is int and node.value == _BROKER_PORT:
                    found.append(f"{rel}:{node.lineno} literal port {_BROKER_PORT}")
            elif isinstance(node, ast.JoinedStr):
                parts = [
                    p.value
                    for p in node.values
                    if isinstance(p, ast.Constant) and isinstance(p.value, str)
                ]
                joined = "".join(parts)
                if any(lit in joined for lit in _LITERALS) or (
                    "nats://" in joined and ":4222" in joined
                ):
                    found.append(f"{rel}:{node.lineno} formatted broker pattern {joined!r}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name in {"get", "getenv"} and node.args:
                    arg = node.args[0]
                    val = _eval_str_node(arg)
                    if val == "NATS_URL":
                        how = "getenv name" if isinstance(arg, ast.Constant) else "getenv built key"
                        found.append(
                            f"{rel}:{node.lineno} reads $NATS_URL via {how}, which nothing sets"
                        )
            elif isinstance(node, ast.Subscript):
                key = _eval_str_node(node.slice)
                if key == "NATS_URL":
                    found.append(
                        f"{rel}:{node.lineno} reads $NATS_URL via environ subscript, "
                        "which nothing sets"
                    )
    return found


def test_no_module_pins_a_broker_address():
    """Ensure tests, fixtures, examples, and scripts dial one address."""
    unlisted = [p for p in _discover_target_files() if _rel(p) not in ALLOWED]
    pinned = pinned_broker_addresses(unlisted)
    assert not pinned, (
        "these dial an address of their own instead of the suite's:\n  "
        + "\n  ".join(pinned)
        + f"\n\nDrop the URL and let `ServiceConfig`'s default apply; "
        f"${TEST_BROKER_URL_ENV} moves it. For a raw `nats.connect`, read "
        "`ServiceConfig.model_fields['nats_url'].default` at call time."
    )


def test_CONTROL_the_scan_reads_real_files():
    """Ensure the target discovery sweeps a substantial collection of files."""
    files = _discover_target_files()
    assert len(files) >= 15, f"Expected at least 15 files, found: {files}"


def test_CONTROL_a_pinned_address_is_detected(tmp_path):
    """Detect literal constants, f-strings, ports, and environment reads."""
    module = tmp_path / "test_planted.py"
    module.write_text(
        '"""nats://localhost:4222 in a docstring is prose, not a dial."""\n'
        "from os import environ, getenv\n"
        "import os\n"
        'URL = "nats://localhost:4222"\n'
        'PAIR = ("localhost", 4222)\n'
        'A = getenv("NATS_URL")\n'
        'B = environ["NATS_URL"]\n'
        'C = os.environ.get("NATS" + "_URL")\n'
        'HOST = "127.0.0.1"\n'
        'D = f"nats://{HOST}:4222"\n'
    )
    found = pinned_broker_addresses([module])
    assert len(found) >= 6, found

    # Each detector branch is asserted separately. Reported with one shared
    # string, two of the three $NATS_URL branches could stop firing and an
    # any() over them would still pass.
    for expected in (
        "literal 'nats://",
        "literal port 4222",
        "formatted broker pattern",
        "reads $NATS_URL via getenv name",
        "reads $NATS_URL via environ subscript",
        "reads $NATS_URL via getenv built key",
    ):
        assert any(expected in f for f in found), f"no hit reporting {expected!r}: {found}"

    assert not any("docstring" in f for f in found), found


# Named independently of SWEEP_DIRECTORIES on purpose: this list is what
# notices if that one is narrowed.
REQUIRED_SWEEP_ROOTS = (
    "tests/unit",
    "tests/integration",
    "tests/transport",
    "tests/repo",
    "examples",
    "scripts",
)


def test_the_sweep_reaches_every_root_it_claims():
    """No root may drop out of the sweep without this failing.

    A rule is only as wide as its discovery step, and a narrowed glob reports
    nothing rather than erroring, so the roots are asserted from disk here
    rather than read back out of the sweep's own configuration.
    """
    swept = {_rel(p) for p in _discover_target_files()}

    for root in REQUIRED_SWEEP_ROOTS:
        on_disk = {_rel(p) for p in (REPO / root).rglob("*.py")}
        assert on_disk, f"{root} holds no Python files; this list is stale"
        missing = on_disk - swept
        assert not missing, (
            f"the sweep misses {len(missing)} file(s) under {root}, "
            f"for example {sorted(missing)[:3]}"
        )

    package_tests = {_rel(f) for d in (REPO / "packages").glob("*/tests") for f in d.rglob("*.py")}
    assert package_tests, "no packages/*/tests directories were found; this check is stale"
    missing = package_tests - swept
    assert not missing, (
        f"the sweep misses {len(missing)} file(s) under packages/*/tests, "
        f"for example {sorted(missing)[:3]}"
    )


def test_the_allowlist_names_files_that_still_pin_an_address():
    """Every allowlisted file exists, still pins, and pins exactly as recorded.

    An entry whose file was cleaned up, or whose count drifted, is an exemption
    nobody is watching -- and a per-file exemption with no count would hide a
    new pin added to a file that already had one.
    """
    assert ALLOWED, "the allowlist is empty; delete it and the branch that reads it"

    measured = _hits_by_file()
    for rel, (count, reason) in sorted(ALLOWED.items()):
        assert (REPO / rel).exists(), f"allowlisted file does not exist: {rel}"
        assert isinstance(reason, str) and reason.strip(), f"no reason recorded for {rel}"
        actual = measured.get(rel, 0)
        assert actual == count, (
            f"{rel} pins {actual} address(es), the allowlist records {count}. "
            "Lower the number when you clear one; a new pin needs a deliberate edit."
        )

    unlisted = {rel: n for rel, n in measured.items() if rel not in ALLOWED}
    assert not unlisted, f"these pin an address and are not in the allowlist: {sorted(unlisted)}"

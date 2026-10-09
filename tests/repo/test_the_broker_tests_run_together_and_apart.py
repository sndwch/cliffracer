"""The broker tests run in one session with the root suite and in each package on its own.

Two things broke that, and each left the other half of the suite unable to say so:

* A root broker test that called `monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", ...)` left the
  prefix set after it. The isolation fixture restored the variable, and then `monkeypatch`, which
  the root conftest's own autouse fixture had asked for first and so undoes last, put back the value
  it had seen. Root tests never noticed, because the next one resets the variable. A test outside
  `tests/` has no such fixture and inherited it, so the metrics pool tests sent a bare subject to a
  service listening under the leaked prefix (`NoRespondersError`) and the distributed cron test
  looked up a lock under the wrong name.
* A package test imported the root `tests` package, which resolves only when the root suite is
  collected in the same session. The package's tests could not be collected on their own.

The first is reproduced here with the real fixtures and no broker: a nested pytest run whose tree
has the root conftest's shape, a broker test that changes the prefix itself, then a test with no
isolation fixture that reads the variable.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
PREFIX_ENV = "CLIFFRACER_SUBJECT_PREFIX"

ROOT_CONFTEST = '''
import pytest


@pytest.fixture(autouse=True)
def _asked_for_first(monkeypatch):
    """Like the repository's root conftest: `monkeypatch` is created before the isolation fixture."""
'''

ISOLATED_CONFTEST = """
from tests.conftest import _broker_namespace, _isolate_broker_tests, _module_namespace  # noqa: F401
"""

HAND_RESTORING_CONFTEST = '''
import os

import pytest

from tests.conftest import _broker_namespace, _module_namespace, _test_talks_to_a_broker  # noqa: F401


@pytest.fixture(autouse=True)
def _isolate_broker_tests(request, _module_namespace):
    """The fixture as it was: it restores the variable by hand."""
    from tests.broker_isolation import PREFIX_ENV

    previous = os.environ.get(PREFIX_ENV)
    wanted = _module_namespace if _test_talks_to_a_broker(request.node) else None
    if wanted is None:
        os.environ.pop(PREFIX_ENV, None)
    else:
        os.environ[PREFIX_ENV] = wanted
    try:
        yield wanted
    finally:
        if previous is None:
            os.environ.pop(PREFIX_ENV, None)
        else:
            os.environ[PREFIX_ENV] = previous
'''

BROKER_TEST_THAT_CHANGES_THE_PREFIX = """
import pytest


@pytest.mark.nats_required
def test_a_broker_test_that_sets_the_prefix_itself(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "west")
"""

TEST_OUTSIDE_THE_ISOLATION = """
import os


def test_it_inherits_no_prefix():
    assert "CLIFFRACER_SUBJECT_PREFIX" not in os.environ, os.environ["CLIFFRACER_SUBJECT_PREFIX"]
"""


def _run_nested(
    tmp_path: Path, isolated_test: str, conftest: str = ISOLATED_CONFTEST
) -> subprocess.CompletedProcess[str]:
    """Run a two-directory tree: a root-shaped broker test, then one with no isolation fixture."""
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    nats_required\n")
    (tmp_path / "conftest.py").write_text(ROOT_CONFTEST)
    (tmp_path / "a_isolated").mkdir()
    (tmp_path / "a_isolated" / "conftest.py").write_text(conftest)
    (tmp_path / "a_isolated" / "test_a.py").write_text(isolated_test)
    (tmp_path / "b_outside").mkdir()
    (tmp_path / "b_outside" / "test_b.py").write_text(TEST_OUTSIDE_THE_ISOLATION)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {PREFIX_ENV, "CLIFFRACER_TEST_NATS_URL", "CLIFFRACER_TEST_NATS_MONITOR_URL"}
    }
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q", "-p", "no:cacheprovider",
         "--import-mode=importlib", "-p", "no:randomly"],
        capture_output=True, text=True, timeout=120, cwd=REPO, env=env,
    )  # fmt: skip


def test_a_broker_test_that_sets_the_prefix_leaves_none_behind_it(tmp_path):
    done = _run_nested(tmp_path, BROKER_TEST_THAT_CHANGES_THE_PREFIX)

    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-500:]
    assert "2 passed" in done.stdout, done.stdout[-500:]


def test_CONTROL_the_nested_run_sees_the_leak_the_fixture_used_to_have(tmp_path):
    """The same tree with the fixture as it was: otherwise the test above passes whatever it does."""
    done = _run_nested(tmp_path, BROKER_TEST_THAT_CHANGES_THE_PREFIX, HAND_RESTORING_CONFTEST)

    assert done.returncode != 0, done.stdout[-800:]
    assert "1 failed, 1 passed" in done.stdout, done.stdout[-800:]
    assert "test_it_inherits_no_prefix" in done.stdout, done.stdout[-800:]


def imports_the_root_tests_package(source: str) -> list[int]:
    """Lines of `import tests...` or `from tests... import`, the repository's root test package."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module == "tests" or module.startswith("tests."):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tests" or alias.name.startswith("tests."):
                    lines.append(node.lineno)
    return sorted(lines)


def test_no_package_test_imports_the_root_tests_package():
    """A package's tests live and run with the package; the root `tests` is not on their path."""
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for path in sorted(REPO.glob("packages/*/tests/**/*.py"))
        for line in imports_the_root_tests_package(path.read_text())
    ]

    assert not offenders, (
        "package tests that import the root `tests` package cannot be collected on their own: "
        f"{offenders}"
    )


def test_CONTROL_the_scan_finds_each_spelling_of_the_import():
    source = (
        "from tests.fixtures.x import y\nimport tests.broker_isolation\n"
        "from tests import conftest\nimport os\nfrom cliffracer.tests import z\n"
    )

    assert imports_the_root_tests_package(source) == [1, 2, 3]


def test_every_package_collects_its_tests_on_its_own():
    """The collection the failing invocation does: the package tests, without the root suite."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         *sorted(str(p) for p in REPO.glob("packages/*/tests"))],
        capture_output=True, text=True, timeout=300, cwd=REPO,
    )  # fmt: skip

    assert done.returncode == 0, (done.stdout + done.stderr)[-1500:]
    assert "error" not in done.stdout.splitlines()[-1].lower(), done.stdout[-500:]

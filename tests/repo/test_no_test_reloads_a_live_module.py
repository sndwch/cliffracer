"""Verify no test reloads a module the running session still holds.

`importlib.reload` re-executes a module body in place. Every object built at
module scope is rebuilt, while any importer that bound one of those objects by
name keeps the original. A `ContextVar`, a registry dict, a sentinel or a
logger reloaded this way leaves two objects where the code assumes one, and the
half that a test later reads is not the half the code under test writes. The
failures land in whatever runs next, not in the test that called reload, so the
cost is paid by an unrelated module and the cause is invisible from its output.

To assert on a module's import-time behaviour, import it in a subprocess and
report the result back; `tests/repo/test_pydantic_v2_compliance.py` carries that
shape. A fresh interpreter gives a genuine first import and rebinds nothing in
the session doing the asserting.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


def suite_modules() -> list[Path]:
    """Return every module the test session imports, conftest files included.

    The root ``conftest.py`` matters most: pytest imports it for every run, so
    a reload there rebinds for the whole session and for every tier at once.
    """
    roots = [REPO / "tests"] + sorted(p for p in REPO.glob("packages/*/tests") if p.is_dir())
    found: list[Path] = []
    for root in roots:
        found.extend(sorted(root.rglob("*.py")))
    root_conftest = REPO / "conftest.py"
    if root_conftest.is_file():
        found.append(root_conftest)
    return sorted(set(found))


def tracked_suite_modules() -> set[str]:
    """The same set as git sees it, so the floor does not count what the sweep counts."""
    out = subprocess.run(
        [
            "git",
            "-C",
            str(REPO),
            "ls-files",
            "tests/*.py",
            "tests/**/*.py",
            "packages/*/tests/*.py",
            "packages/*/tests/**/*.py",
            "conftest.py",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return set(out)


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def _reload_calls(source: str, filename: str) -> list[int]:
    """Return the line of every importlib.reload call in `source`.

    Both spellings count: the attribute call on an imported `importlib`, and a
    bare `reload(...)` bound by `from importlib import reload`.
    """
    tree = ast.parse(source, filename=filename)

    # `import importlib as _il` renames the module, `from importlib import
    # reload as _r` renames the function. Both spell the same call.
    module_names: set[str] = {"importlib"}
    bare_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "reload":
                    bare_names.add(alias.asname or alias.name)

    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "reload":
            root = func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in module_names:
                lines.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in bare_names:
            lines.append(node.lineno)
    return lines


def reloading_tests() -> list[str]:
    """Return `path:line` for every reload call in the suite."""
    offenders: list[str] = []
    for path in suite_modules():
        for line in _reload_calls(path.read_text(), str(path)):
            offenders.append(f"{_rel(path)}:{line}")
    return offenders


def test_no_test_module_reloads_a_module() -> None:
    offenders = reloading_tests()
    assert offenders == [], (
        "these reload a module the session still holds, which rebinds its "
        "module-level objects for every later test: "
        + ", ".join(offenders)
        + ". Import the module in a subprocess instead."
    )


def test_the_sweep_reads_every_module_git_tracks() -> None:
    """A sweep that quietly stopped reaching a directory would pass the check above.

    The expectation comes from ``git ls-files`` rather than from another walk of
    the same tree, so the floor cannot drift down with the sweep it guards.
    """
    swept = {_rel(p) for p in suite_modules()}
    tracked = tracked_suite_modules()
    missing = sorted(tracked - swept)
    assert missing == [], f"the sweep does not reach {len(missing)} tracked modules: {missing[:10]}"
    assert "conftest.py" in swept, "the root conftest is not swept"
    assert any(m.startswith("packages/") for m in swept), "packages/ not reached"
    assert any(m.startswith("tests/repo/") for m in swept), "tests/repo/ not reached"


def test_CONTROL_a_reload_in_a_root_conftest_is_named(tmp_path: Path) -> None:
    """The root conftest is the worst place for a reload and the easiest to miss."""
    conftest = tmp_path / "conftest.py"
    conftest.write_text("import importlib\nimport json\nimportlib.reload(json)\n")
    assert _reload_calls(conftest.read_text(), str(conftest)) == [3]


def test_CONTROL_an_attribute_reload_is_named() -> None:
    src = "import importlib\nmod = importlib.import_module('json')\nimportlib.reload(mod)\n"
    assert _reload_calls(src, "sample.py") == [3]


def test_CONTROL_a_bare_reload_import_is_named() -> None:
    """`from importlib import reload` hides the call behind a plain name."""
    src = "from importlib import reload\nimport json\nreload(json)\n"
    assert _reload_calls(src, "sample.py") == [3]


def test_CONTROL_an_aliased_importlib_is_named() -> None:
    """`import importlib as _il` renames the module, not the call."""
    src = "import importlib as _il\nimport json as _j\n_il.reload(_j)\n"
    assert _reload_calls(src, "sample.py") == [3]


def test_CONTROL_an_aliased_bare_reload_is_named() -> None:
    """`from importlib import reload as _r` renames the function."""
    src = "from importlib import reload as _r\nimport json\n_r(json)\n"
    assert _reload_calls(src, "sample.py") == [3]


def test_CONTROL_the_subprocess_shape_is_not_flagged() -> None:
    """The sanctioned alternative must survive the sweep.

    This is the shape the deprecation check uses: a fresh interpreter does the
    importing, so nothing in the asserting session is rebound.
    """
    src = (
        "import subprocess\n"
        "import sys\n"
        "probe = 'import importlib; importlib.import_module(sys.argv[1])'\n"
        "subprocess.run([sys.executable, '-c', probe, 'json'], check=True)\n"
    )
    assert _reload_calls(src, "sample.py") == []


def test_CONTROL_an_unrelated_reload_method_is_not_flagged() -> None:
    """Only importlib's reload counts; a `reload` method on something else is
    an ordinary call and must not be swept up."""
    src = "cache.reload()\nself.reload(config)\nwidget.importlib = 1\n"
    assert _reload_calls(src, "sample.py") == []


def test_the_subprocess_form_it_recommends_actually_runs() -> None:
    """The message sends readers to a subprocess import, so one must work."""
    probe = "import importlib, sys; importlib.import_module(sys.argv[1]); print('ok')"
    result = subprocess.run(
        [sys.executable, "-c", probe, "cliffracer.core.messages"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout

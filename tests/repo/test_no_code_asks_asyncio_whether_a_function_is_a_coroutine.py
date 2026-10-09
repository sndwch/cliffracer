"""Guard: nothing calls `asyncio.iscoroutinefunction`, which Python 3.16 removes.

It is deprecated from 3.14, warns on every call there, and is gone in 3.16; the supported
interpreters have no upper bound, so a call left in a runtime path warns now and fails later.
`inspect.iscoroutinefunction` is the replacement and answers the same for every handler shape the
framework dispatches (`tests/unit/test_a_timer_fires_on_an_interpreter_without_asyncio_iscoroutinefunction.py`
runs the timer with the asyncio one removed).

Read by syntax tree across the shipped sources, the packages' tests, the repository's tests, the
examples, the scripts and the load tests: an attribute `iscoroutinefunction` on `asyncio` or a name
`asyncio` is imported under, and an import of it from `asyncio` or `asyncio.coroutines`.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
TREES = ("src", "packages", "tests", "examples", "scripts", "load-testing")


def python_files() -> list[Path]:
    return sorted(
        path
        for tree in TREES
        for path in (REPO / tree).rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts
    )


def asyncio_coroutine_checks(source: str) -> list[tuple[int, str]]:
    """Each use of `asyncio.iscoroutinefunction` in `source`, as (line, how it is spelled)."""
    tree = ast.parse(source)
    names = {"asyncio"} | {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "asyncio"
    }
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "iscoroutinefunction"
            and isinstance(node.value, ast.Name)
            and node.value.id in names
        ):
            found.append((node.lineno, f"{node.value.id}.iscoroutinefunction"))
        elif isinstance(node, ast.ImportFrom) and node.module in ("asyncio", "asyncio.coroutines"):
            if any(alias.name == "iscoroutinefunction" for alias in node.names):
                found.append((node.lineno, f"from {node.module} import iscoroutinefunction"))
    return sorted(found)


def test_no_code_asks_asyncio_whether_a_function_is_a_coroutine():
    found = {
        f"{path.relative_to(REPO)}:{line}": how
        for path in python_files()
        for line, how in asyncio_coroutine_checks(path.read_text())
    }

    assert not found, f"use inspect.iscoroutinefunction instead: {found}"


def test_the_sweep_reads_the_trees_it_names():
    files = python_files()
    for tree in ("src", "packages", "tests"):
        assert any(p.is_relative_to(REPO / tree) for p in files), f"no files read under {tree}/"
    assert len(files) >= 500, f"only {len(files)} files read; is the sweep looking in the repo?"


@pytest.mark.parametrize(
    ("source", "found"),
    [
        pytest.param(
            "import asyncio\nasyncio.iscoroutinefunction(f)\n",
            [(2, "asyncio.iscoroutinefunction")],
            id="called",
        ),
        pytest.param(
            "import asyncio as aio\nok = aio.iscoroutinefunction\n",
            [(2, "aio.iscoroutinefunction")],
            id="aliased-and-not-called",
        ),
        pytest.param(
            "from asyncio import iscoroutinefunction\n",
            [(1, "from asyncio import iscoroutinefunction")],
            id="imported",
        ),
        pytest.param(
            "from asyncio.coroutines import iscoroutinefunction as icf\n",
            [(1, "from asyncio.coroutines import iscoroutinefunction")],
            id="imported-from-coroutines",
        ),
    ],
)
def test_CONTROL_each_spelling_is_found(source, found):
    assert asyncio_coroutine_checks(source) == found


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import inspect\ninspect.iscoroutinefunction(f)\n", id="inspects"),
        pytest.param("helper.iscoroutinefunction(f)\n", id="another-objects-method"),
        pytest.param('monkeypatch.setattr(asyncio, "iscoroutinefunction", f)\n', id="a-string"),
    ],
)
def test_CONTROL_what_is_not_asyncios_is_not_found(source):
    assert asyncio_coroutine_checks(source) == []

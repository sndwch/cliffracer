"""Every place that states the minimum Python states the same one, and the code parses on it.

`requires-python` is what an installer reads, but it is not what decides: the
source does. A declared floor below the syntax the source uses installs cleanly
and fails at the first import, and CI runs one newer interpreter, so nothing
executes the floor itself. `ast.parse(feature_version=...)` asks the running
interpreter's parser to refuse syntax newer than a given version, which lets a
single CI interpreter check the declared floor. It is best-effort by design --
it covers syntax such as type parameter lists and `type` statements, not
standard-library APIs -- so this is a lower bound on what the floor must be.
"""

import ast
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

PYPROJECTS = sorted(
    [REPO / "pyproject.toml", REPO / "example_consumer" / "pyproject.toml"]
    + list((REPO / "packages").glob("*/pyproject.toml"))
)

# The documents that show a Dockerfile. The tree carries no Dockerfile of its own.
DOCKERFILE_TEXTS = sorted([REPO / "README.md", REPO / "docs" / "ARCHITECTURE.md"])

# Any image whose name ends in `python:3.N`, such as `python:3.N-slim`.
FROM_PYTHON = re.compile(r"^FROM \S*python:3\.(\d+)", re.M)
STATED_FLOOR = re.compile(r'requires-python\s*=\s*"(>=3\.\d+)"')
BADGE = re.compile(r"\[!\[Python 3\.(\d+)\+\]\(https://img\.shields\.io/badge/python-3\.(\d+)%2B")


def _minor(spec: str) -> int:
    match = re.fullmatch(r">=3\.(\d+)", spec)
    assert match, f"requires-python {spec!r} is not a plain >=3.N floor"
    return int(match.group(1))


def _floor() -> int:
    return _minor(
        tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["requires-python"]
    )


def _sources() -> list[Path]:
    roots = [REPO / "src"] + sorted(p for p in (REPO / "packages").glob("*/src") if p.is_dir())
    return sorted(path for root in roots for path in root.rglob("*.py"))


def _too_new(paths: list[Path], minor: int) -> list[str]:
    refused = []
    for path in paths:
        try:
            ast.parse(path.read_text(), str(path), feature_version=(3, minor))
        except SyntaxError as exc:
            refused.append(f"{path}:{exc.lineno}: {exc.msg}")
    return refused


def _stated_floors() -> dict[str, list[str]]:
    """Every `requires-python = "..."` in a tracked file, not only in pyproject files.

    A script that generates a pyproject from a heredoc states a floor too, and
    running it writes that floor back over the one the pyproject test reads.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    found: dict[str, list[str]] = {}
    for rel in filter(None, tracked):
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if specs := STATED_FLOOR.findall(text):
            found[rel] = specs
    return found


@pytest.mark.parametrize("pyproject", PYPROJECTS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_distribution_declares_the_same_floor(pyproject: Path):
    declared = tomllib.loads(pyproject.read_text())["project"]["requires-python"]

    assert _minor(declared) == _floor(), f"{pyproject.relative_to(REPO)} declares {declared}"


def test_every_stated_requires_python_in_the_tree_is_the_floor():
    found = _stated_floors()

    assert set(map(str, (p.relative_to(REPO) for p in PYPROJECTS))) <= set(found), (
        "the sweep no longer finds the pyproject files it must: " + ", ".join(sorted(found))
    )
    wrong = {rel: specs for rel, specs in found.items() if {_minor(s) for s in specs} != {_floor()}}
    assert wrong == {}, wrong


def test_the_lowest_classifier_is_the_floor():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    minors = [
        int(m.group(1))
        for c in project["classifiers"]
        if (m := re.fullmatch(r"Programming Language :: Python :: 3\.(\d+)", c))
    ]

    assert minors, "no Python 3.N classifier found"
    assert min(minors) == _floor(), sorted(minors)


def test_the_readme_badge_states_the_floor():
    found = BADGE.findall((REPO / "README.md").read_text())

    assert found, "the Python badge is no longer where this test looks"
    assert all(int(label) == int(url) == _floor() for label, url in found), found


@pytest.mark.parametrize("path", DOCKERFILE_TEXTS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_python_base_image_is_the_floor(path: Path):
    minors = [int(m) for m in FROM_PYTHON.findall(path.read_text())]

    assert minors, f"{path.relative_to(REPO)} has no `FROM python:3.N` line"
    assert set(minors) == {_floor()}, minors


def test_the_source_parses_at_the_declared_floor():
    sources = _sources()

    assert len(sources) > 50, f"found only {len(sources)} source files"
    assert _too_new(sources, _floor()) == []


def test_CONTROL_the_parse_refuses_syntax_newer_than_its_version(tmp_path: Path):
    """The check above can fail: the version below the floor refuses the floor's syntax.

    Written as a synthetic file, not read off the tree, so removing a generic
    from the source does not turn a correct tree red.
    """
    newer = tmp_path / "newer.py"
    newer.write_text("class Box[T]:\n    pass\n")

    assert _too_new([newer], 12) == []
    refused = _too_new([newer], 11)
    assert len(refused) == 1 and "3.12" in refused[0], refused

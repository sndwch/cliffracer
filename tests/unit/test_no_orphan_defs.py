"""Module-level functions and classes that nothing references.

This sweep fails the build on any function or class at module level
that is never referenced across the repository.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "packages", "tests", "examples", "tools")
# tools/ is included because generators and one-shot utility scripts reside there.


def _py_files(root: Path):
    for d in SCAN_DIRS:
        base = root / d
        if base.exists():
            yield from base.rglob("*.py")


# THE ONE EXEMPTION, AS A RULE RATHER THAN A LIST OF NAMES. A name list needs
# editing whenever a class is added and fails open when nobody remembers; a rule
# states the property that makes the class reachable, so a new class either has
# that property or is swept.
#
# pytest collects `Test*` classes by convention, so nothing references them by
# name. Restricted to files under a tests/ directory: a `TestHarness` in src/ is
# a real orphan, and a bare prefix rule would exempt it.
#
# The report filter below keeps only files under src/, packages/ and tools/, so
# test classes in the root tests/ tree are out of scope and never reach this
# exemption. The rule applies to classes under packages/*/tests/.
#
# No exemption is needed for exported exceptions: package __init__ files that
# export exception types import them, and imports count as references.
# test_an_exported_exception_is_reachable_because_the_export_is_a_reference
# verifies this behavior.


def _in_tests_dir(path: Path) -> bool:
    return "tests" in path.parts


def _defs(path: Path):
    """Module-level defs AND classes, minus the one exempt shape."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node.lineno, node.name
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("Test") and _in_tests_dir(path):
                continue
            yield node.lineno, node.name


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add((node.asname or node.name).split(".")[-1])
    return out


def orphan_defs(root: Path) -> list[tuple[Path, int, str]]:
    files = list(_py_files(root))
    refs_by_file = {f: _names(f) for f in files}
    orphans = []
    for f in files:
        # WHERE ORPHANS ARE REPORTED, which is not the same as where names are
        # counted. Every SCAN_DIR contributes references; only these are
        # searched for orphans. Adding a directory to SCAN_DIRS alone makes it
        # a referencer and never a subject -- which is what "add tools/" looked
        # like it did, and did not.
        if not any(str(f).startswith(str(root / d)) for d in ("src", "packages", "tools")):
            continue
        for line, name in _defs(f):
            if name.startswith("_") or name.startswith("test_"):
                continue
            referenced = any(
                name in refs for other, refs in refs_by_file.items() if other != f
            ) or _referenced_in_own_file(f, name)
            if not referenced:
                orphans.append((f.relative_to(root), line, name))
    return orphans


def _referenced_in_own_file(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text(), filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            count += 1
        if isinstance(node, ast.Attribute) and node.attr == name:
            count += 1
    # __all__ entries count as a reference: they are the public surface.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == name:
            count += 1
    return count > 0


@pytest.mark.unit
def test_the_sweep_finds_a_planted_orphan(tmp_path: Path):
    """Positive control: the instrument must be able to fail."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "def used():\n    pass\n\ndef orphan():\n    pass\n\nused()\n"
    )
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/mod.py", 4, "orphan")]


@pytest.mark.unit
def test_the_sweep_finds_a_planted_orphan_class(tmp_path: Path):
    """Positive control for the widening: a class nothing names is an orphan.

    The def half has its own control above. Widening the walk without one would
    leave the class half asserted by a check nothing had shown could fail for
    classes -- which is the same gap as a licence check whose control removes
    one member's file.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "class Used:\n    pass\n\n\nclass Orphan:\n    pass\n\n\nUsed()\n"
    )
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/mod.py", 5, "Orphan")]


@pytest.mark.unit
def test_the_Test_exemption_does_not_reach_outside_a_tests_directory(tmp_path: Path):
    """The exemption's own negative: a bare `Test*` rule would exempt src/ too.

    An exemption that exempts more than its reason covers is worse than none,
    because it is invisible: `TestHarness` in src/ is a real orphan and pytest
    is not collecting it.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("class TestHarness:\n    pass\n")
    (tmp_path / "packages" / "p" / "tests").mkdir(parents=True)
    (tmp_path / "packages" / "p" / "tests" / "test_x.py").write_text("class TestThing:\n    pass\n")

    found = {name for _, _, name in orphan_defs(tmp_path)}
    assert "TestHarness" in found, (
        "a Test* class in src/ is not collected by pytest and is an orphan"
    )
    assert "TestThing" not in found, "a Test* class under tests/ is collected by convention"


@pytest.mark.unit
def test_an_exported_exception_is_reachable_because_the_export_is_a_reference(tmp_path: Path):
    """Why there is no exemption for exported exceptions -- asserted, not assumed.

    "An exception exists to be caught, so exported ones are exempt" reads like a
    rule this sweep needs, and it is inert: the __init__ that exports a name
    IMPORTS it, and an import is a reference, so an exported exception never
    reaches the orphan list to be exempted from it. This pins the property the
    absent exemption relies on, so a change to _names() that stopped counting
    aliases would fail here rather than silently start flagging every exported
    exception in the tree.
    """
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "exceptions.py").write_text(
        "class Exported(Exception):\n    pass\n\n\nclass Unexported(Exception):\n    pass\n"
    )
    (tmp_path / "src" / "pkg" / "__init__.py").write_text(
        'from pkg.exceptions import Exported\n\n__all__ = ["Exported"]\n'
    )

    found = {name for _, _, name in orphan_defs(tmp_path)}
    assert "Exported" not in found, "the export is the reference; no exemption is needed"
    assert "Unexported" in found, (
        "negative half: an exception nothing raises and nothing exports cannot "
        "be caught either, which is what makes unexported sufficient"
    )


@pytest.mark.unit
def test_no_module_level_definition_is_unreferenced():
    found = orphan_defs(REPO)
    assert not found, "unreferenced module-level defs:\n" + "\n".join(
        f"  {f}:{line} {name}" for f, line, name in found
    )

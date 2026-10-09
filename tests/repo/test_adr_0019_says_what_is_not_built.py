"""ADR-0019 is Proposed and says none of its invariants is implemented; this fails when that stops being true.

The record names a `cliffracer-actors` extension and two errors (`ClientActorActivating`,
`ActorCycle`), and its Implementation line says none of it exists. That line is a claim
about the repository, so it is checked against it, as ADR-0018's is: when one of the pieces appears
the line is false, and this names the one to update. Whether the package exists is held, with the
status, by `test_adr_status_tracks_package_presence.py`; this reads the rest.
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
NAMED_ERRORS = {"ClientActorActivating", "ActorCycle"}


def adr_0019(text: str) -> str:
    match = re.search(r"^## ADR-0019\b.*?(?=^## ADR-|\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, "ADR-0019 is not in decisions.md"
    return match.group(0)


def defined_errors(root: Path) -> set[str]:
    """The named errors that a class statement defines anywhere under the source trees."""
    found: set[str] = set()
    sources = [root / "src", *sorted((root / "packages").glob("*/src"))]
    for tree_root in sources:
        for path in tree_root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(), str(path))):
                if isinstance(node, ast.ClassDef) and node.name in NAMED_ERRORS:
                    found.add(node.name)
    return found


def _section() -> str:
    return adr_0019((REPO / "docs" / "decisions.md").read_text())


def test_the_record_is_proposed_and_has_an_implementation_line():
    section = _section()

    assert re.search(r"^- \*\*Status\*\*: Proposed\s*$", section, flags=re.MULTILINE)
    assert re.search(
        r"^- \*\*Implementation\*\*: None of the six invariants is implemented", section, re.M
    )


def test_the_two_errors_it_names_are_defined_nowhere_while_it_says_so():
    assert defined_errors(REPO) == set(), (
        f"{sorted(defined_errors(REPO))} is now defined: update ADR-0019's Implementation line"
    )


def test_CONTROL_a_class_with_a_named_error_is_seen(tmp_path):
    source = tmp_path / "packages" / "cliffracer-actors" / "src" / "cliffracer_actors"
    source.mkdir(parents=True)
    (source / "errors.py").write_text("class ActorCycle(Exception):\n    pass\n")

    assert defined_errors(tmp_path) == {"ActorCycle"}

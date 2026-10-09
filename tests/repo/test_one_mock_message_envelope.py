"""One class named MockMessage, so a test asserts against the envelope it means.

A second class of the same name does not collide at import -- each module gets
the one its own imports resolve -- so the two diverge quietly and a reader
checking one has checked neither. The suite carried two for a while: the shipped
envelope, and a test-local one with no acknowledgement surface at all. Between
them they produced two incorrect readings in one day, one of them a filed issue
that had to be withdrawn.

The sweep reads class definitions, so a copy reintroduced anywhere under the
tracked tree fails here rather than waiting to mislead someone.
"""

import ast
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
ENVELOPE = "MockMessage"

# The one definition, which everything else imports.
HOME = "src/cliffracer/testing/messages.py"


def tracked_python_files() -> list[Path]:
    """Every tracked .py file, read from git rather than from a glob."""
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [REPO / line for line in out.splitlines() if line.strip()]


def definitions_of(name: str, files: list[Path]) -> list[str]:
    """`path:line` for every class definition of *name*."""
    found = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:  # pragma: no cover - a file that does not parse is not ours to judge
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == name:
                # Relative for a tracked file, absolute otherwise: the control
                # below hands this files under tmp_path, and relative_to raises
                # for anything outside the repository.
                label = path.relative_to(REPO) if path.is_relative_to(REPO) else path
                found.append(f"{label}:{node.lineno}")
    return sorted(found)


def test_the_envelope_is_defined_once():
    """Exactly one class named MockMessage, and it is the shipped one."""
    files = tracked_python_files()
    assert files, "git ls-files returned nothing, so this sweep read no source at all"

    definitions = definitions_of(ENVELOPE, files)

    assert len(definitions) == 1, (
        f"{len(definitions)} classes are named {ENVELOPE}: {definitions}. Two envelopes "
        "of one name diverge quietly, and a test asserting against one proves nothing "
        "about the other."
    )
    assert definitions[0].startswith(HOME), (
        f"the only {ENVELOPE} is at {definitions[0]}, not {HOME}"
    )


def test_CONTROL_the_sweep_finds_a_second_definition(tmp_path: Path):
    """The reader reports a duplicate, rather than only ever finding one."""
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text(f"class {ENVELOPE}:\n    pass\n")
    second.write_text(f"class {ENVELOPE}:\n    pass\n")

    assert len(definitions_of(ENVELOPE, [first, second])) == 2


def test_CONTROL_an_unrelated_class_is_not_counted():
    """A different name is not the envelope, however similar."""
    assert definitions_of(ENVELOPE, tracked_python_files()) != definitions_of(
        "MockJetStreamMsg", tracked_python_files()
    )
    assert definitions_of("MockMessageThatIsNotThisOne", tracked_python_files()) == []

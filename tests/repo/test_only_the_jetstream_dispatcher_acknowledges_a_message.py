"""Which component decides a message's acknowledgement: only the JetStream dispatcher.

ADR-0014 leaves open which layer owns the ack, nak and term decision. The code puts it in one
place: `cliffracer.core.dispatch.jetstream`. The transport-agnostic event dispatcher never touches a
message's acknowledgement, which is what keeps a refusal from being terminated twice. This reads the
source and fails when another module calls `ack`, `nak`, `term` or `in_progress` on anything.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SOURCES = [REPO / "src" / "cliffracer", *sorted((REPO / "packages").glob("*/src"))]
OWNER = REPO / "src" / "cliffracer" / "core" / "dispatch" / "jetstream.py"
ACKNOWLEDGEMENTS = {"ack", "ack_sync", "nak", "term", "in_progress"}


def _calls(path: Path) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ACKNOWLEDGEMENTS
        ):
            found.append((node.lineno, node.func.attr))
    return found


def _files() -> list[Path]:
    return [p for root in SOURCES for p in sorted(root.rglob("*.py"))]


def test_no_module_but_the_jetstream_dispatcher_acknowledges_a_message():
    outside = {
        f"{path.relative_to(REPO)}:{line} .{name}()": None
        for path in _files()
        if path != OWNER
        for line, name in _calls(path)
    }

    assert not outside, f"an acknowledgement outside the JetStream dispatcher: {sorted(outside)}"


def test_CONTROL_the_jetstream_dispatcher_does_acknowledge_and_the_scan_sees_it():
    names = {name for _, name in _calls(OWNER)}

    assert {"nak", "term"} <= names, names
    assert len(_files()) > 50


def test_CONTROL_the_scan_finds_an_acknowledgement_in_any_module(tmp_path):
    planted = tmp_path / "elsewhere.py"
    planted.write_text("async def handle(msg):\n    await msg.term()\n")

    assert _calls(planted) == [(2, "term")]

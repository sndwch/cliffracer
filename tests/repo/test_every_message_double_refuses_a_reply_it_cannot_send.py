"""Every hand-written message double refuses a reply the real `Msg` refuses.

`nats.aio.msg.Msg.respond` raises when there is no reply subject: a
fire-and-forget message cannot be replied to. A double that records the reply
instead lets a test assert a successful response that production would have
refused, which is the defect the shipped `MockMessage` was fixed for.

THE SAME CONTRACT HAD THREE SPELLINGS AND TWELVE ABSENCES. One double checked
`self.reply` and raised `RuntimeError`; the shipped mock raised
`nats.errors.Error`, which is what the real one raises; twelve recorded
unconditionally. So the rule now lives once, as
`cliffracer.testing.refuse_a_reply_with_no_subject`, and this asserts every
double reaches it -- because a rule that has to be remembered at fifteen call
sites is a rule that will be missing at the sixteenth.

WHY A SWEEP RATHER THAN A TEST PER DOUBLE: their constructors disagree (a dict
here, bytes there, a positional subject, a keyword), so instantiating all of
them from one test means fifteen special cases that rot. What is checkable
uniformly is that the call is present, which is what this reads.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
RULE = "refuse_a_reply_with_no_subject"

#: The doubles that answer for themselves, with the reason.
EXEMPT_REASONS: dict[str, str] = {
    "src/cliffracer/testing/messages.py::MockMessage": (
        "Defines the rule. Its own `respond` calls it, which this sweep sees; "
        "listed so the count below reads as deliberate rather than lucky."
    ),
}


def _roots() -> list[Path]:
    return [REPO / "tests", REPO / "packages", REPO / "src"]


def doubles() -> list[tuple[str, str, bool]]:
    """Every class defining `respond`, and whether its body reaches the rule."""
    found = []
    for root in _roots():
        for path in sorted(root.rglob("*.py")):
            if ".venv" in path.parts:
                continue
            text = path.read_text()
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                respond = next(
                    (
                        n
                        for n in node.body
                        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                        and n.name == "respond"
                    ),
                    None,
                )
                if respond is None:
                    continue
                calls_rule = any(
                    isinstance(call.func, ast.Name) and call.func.id == RULE
                    for call in ast.walk(respond)
                    if isinstance(call, ast.Call)
                )
                rel = str(path.relative_to(REPO))
                found.append((f"{rel}::{node.name}", rel, calls_rule))
    return found


def test_the_sweep_finds_the_doubles():
    """A sweep that finds nothing passes every assertion below."""
    found = doubles()

    assert len(found) >= 15, f"only found {len(found)} classes defining respond: {found}"


def test_every_double_reaches_the_one_reply_rule():
    """The rule is one function, and every `respond` in the tree calls it."""
    missing = sorted(name for name, _, calls in doubles() if not calls)

    assert missing == [], (
        "these message doubles record a reply without checking for a reply "
        "subject, so a test using one can assert a response the real `Msg` "
        f"would have refused. Call `{RULE}` from "
        f"`cliffracer.testing` at the top of `respond`:\n  " + "\n  ".join(missing)
    )


def test_CONTROL_the_rule_is_one_function_in_one_place():
    """Otherwise "every double calls the rule" is satisfied by several rules."""
    definitions = []
    for root in _roots():
        for path in sorted(root.rglob("*.py")):
            if ".venv" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == RULE:
                    definitions.append(str(path.relative_to(REPO)))

    # The file, not the line: a line number breaks on any edit above it and
    # would have to be corrected by whoever adds an import.
    assert definitions == ["src/cliffracer/testing/messages.py"], definitions


def test_CONTROL_a_double_that_does_not_call_it_is_reported():
    """The detector, against a known-bad shape.

    Written as source and parsed, because the sweep reads source: a fixture
    object would test something else.
    """
    bad = ast.parse(
        "class Sloppy:\n    async def respond(self, data):\n        self.responded = data\n"
    )
    good = ast.parse(
        "class Careful:\n"
        "    async def respond(self, data):\n"
        f"        {RULE}(self)\n"
        "        self.responded = data\n"
    )

    def reaches(tree):
        cls = tree.body[0]
        respond = cls.body[0]
        return any(
            isinstance(c.func, ast.Name) and c.func.id == RULE
            for c in ast.walk(respond)
            if isinstance(c, ast.Call)
        )

    assert reaches(good), "the detector misses a double that DOES call the rule"
    assert not reaches(bad), "the detector passes a double that does not call it"


def test_every_exemption_names_a_double_that_exists():
    names = {name for name, _, _ in doubles()}
    unknown = sorted(set(EXEMPT_REASONS) - names)

    assert not unknown, f"exemptions naming no double: {unknown}"


def test_every_exemption_has_a_reason():
    for name, reason in EXEMPT_REASONS.items():
        assert isinstance(reason, str) and reason.strip(), name


# --- the wiring, not only the call ------------------------------------------
#
# The sweep above reads source: it proves the call is written, not that it
# fires. These drive three doubles of different shapes -- a keyword `reply`, a
# class attribute, and one whose reply defaults to None already -- and assert
# the refusal actually reaches a caller. Three rather than fifteen, because the
# rule is one function: what varies between doubles is the wiring, and three
# shapes is every shape there is.


@pytest.mark.asyncio
async def test_a_double_with_a_reply_keyword_refuses_when_it_is_none():
    from nats.errors import Error

    from tests.unit.test_rpc_error_envelopes import MockRpcMsg

    msg = MockRpcMsg("svc.rpc.x", b"{}", reply="")

    with pytest.raises(Error, match="no reply subject"):
        await msg.respond(b"{}")

    assert msg.response_bytes is None, "a refused reply must not be recorded"


@pytest.mark.asyncio
async def test_a_double_carrying_the_rule_as_a_class_attribute_refuses_too():
    """The five doubles that had no `reply` at all got one as a class
    attribute, so a test can override it per instance without a constructor
    argument the double never had."""
    from nats.errors import Error

    from tests.unit.test_rpc_envelope import _MockMsg

    msg = _MockMsg("svc.rpc.x", {})
    msg.reply = None

    with pytest.raises(Error, match="no reply subject"):
        await msg.respond(b"{}")

    assert msg.response is None


@pytest.mark.asyncio
async def test_CONTROL_the_same_doubles_still_record_a_reply_they_can_send():
    """Otherwise "it refuses" would be satisfied by refusing everything, and
    every test that asserts a response would have reded instead of these."""
    from tests.unit.test_rpc_envelope import _MockMsg
    from tests.unit.test_rpc_error_envelopes import MockRpcMsg

    keyworded = MockRpcMsg("svc.rpc.x", b"{}")
    await keyworded.respond(b'{"ok": true}')
    assert keyworded.response_bytes == b'{"ok": true}'

    attributed = _MockMsg("svc.rpc.x", {})
    await attributed.respond(b'{"ok": true}')
    assert attributed.response == {"ok": True}

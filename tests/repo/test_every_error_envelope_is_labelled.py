"""Every error a service sends says what KIND of error it is, in a typed field.

A `ServiceClient` classifies a reply on its `code` and falls back to matching
the `error` prose when there is none. That fallback is documented, in the
CHANGELOG and in `docs/api-reference.md`, as meaning one specific thing:

    A reply with no `code` comes from a service that predates the field and is
    classified by its prefix, as before.

That sentence is true only while every arm of a CURRENT dispatcher labels
itself. If one loses its `code` -- by an edit, or by a new envelope being added
without one -- a current service emits an unlabelled reply, the client silently
falls back to prose, and a live deployment is read as an old one. The defect
being fixed is misclassification, and that is the same defect wearing the fix's
own clothes.

MEASURED, dropping each `code` in turn and running the unit tier: seven of the
nine envelopes could lose it with **nothing red**. Only two were pinned, both
incidentally, by tests that happen to assert on a labelled envelope.

THIS GUARD READS THE TREE, NOT A LIST. The count is asserted so a tenth
envelope has to be looked at rather than silently admitted, and the vocabulary
is compared against the codes the CLIENT actually branches on -- a service
emitting `unknown-method` for `unknown_method` would fall through to prose with
nothing to say so, which is the same failure by a typo instead of an omission.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
# The RPC dispatcher, the module that builds the replies of a request its limits stop, the one
# that ends a streamed reply, and the one that answers describe.
DISPATCHERS = (
    REPO / "src" / "cliffracer" / "core" / "dispatch" / "rpc.py",
    REPO / "src" / "cliffracer" / "core" / "dispatch" / "rpc_limits.py",
    REPO / "src" / "cliffracer" / "core" / "dispatch" / "rpc_stream.py",
    REPO / "src" / "cliffracer" / "core" / "dispatch" / "describe.py",
)
# The one function that reads an envelope, shared by the standalone client and the service's `call_rpc`.
CLIENT = REPO / "src" / "cliffracer" / "core" / "exceptions.py"

# Asserted, not merely counted: one envelope more is a decision, not a detail.
EXPECTED_ENVELOPES = 13


def codes_of(node: ast.expr | None) -> frozenset[str] | None:
    """Every string a `code` entry can evaluate to, or `None` if that is not knowable.

    NOT just `ast.Constant`. A `code` may be a conditional -- the dispatcher
    writes `"internal" if crashed else "refused"` on the arms that tell a
    crashed hook from an authored refusal -- and an arm like that is labelled on
    BOTH branches or on neither. A reader that accepted only a literal called
    both of those unlabelled, which is how this function's first version would
    have reported a false failure the moment it met one.
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset({node.value})
    if isinstance(node, ast.IfExp):
        body, orelse = codes_of(node.body), codes_of(node.orelse)
        if body is None or orelse is None:
            return None
        return body | orelse
    return None


def error_envelopes(source: str) -> list[tuple[int, frozenset[str] | None]]:
    """Every `{"success": False, ...}` dict literal, with the codes it can carry.

    Read as code rather than searched as text: `"code"` appears in this module's
    own prose about the field, and a guard against a construct has to tell the
    construct from writing about it.
    """
    found: list[tuple[int, frozenset[str] | None]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {
            key.value: value
            for key, value in zip(node.keys, node.values, strict=False)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        success = pairs.get("success")
        if not (isinstance(success, ast.Constant) and success.value is False):
            continue
        found.append((node.lineno, codes_of(pairs.get("code"))))
    return found


def codes_the_client_branches_on(source: str) -> set[str]:
    """The literals compared against `code` in the client's classification."""
    understood: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "code"):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                understood.add(comparator.value)
    return understood


def _envelopes() -> list[tuple[str, frozenset[str] | None]]:
    """Every error envelope the dispatch modules build, as ("module:line", its codes)."""
    return [
        (f"{path.name}:{line}", codes)
        for path in DISPATCHERS
        for line, codes in error_envelopes(path.read_text())
    ]


def test_every_error_envelope_carries_a_code():
    """An unlabelled envelope from a current service is read as an old service."""
    envelopes = _envelopes()

    assert len(envelopes) >= 5, f"the sweep found only {len(envelopes)}; is it reading?"
    unlabelled = [line for line, codes in envelopes if not codes]
    assert not unlabelled, (
        "these error envelopes carry no `code` this can read, so a client reads "
        "them as coming from a service that predates the field. A conditional is "
        "fine as long as every branch is a string literal: "
        f"lines {unlabelled}"
    )


def test_the_number_of_error_envelopes_is_the_number_that_was_reviewed():
    """An arm more must be looked at rather than admitted silently."""
    envelopes = _envelopes()

    assert len(envelopes) == EXPECTED_ENVELOPES, (
        f"{len(envelopes)} error envelopes, not {EXPECTED_ENVELOPES}. If you added "
        "one, give it a code the client branches on and raise this number; if you "
        f"removed one, lower it. Found at lines {[line for line, _ in envelopes]}"
    )


def test_every_code_emitted_is_one_the_client_understands():
    """A typo is the same failure as an omission, and quieter.

    `unknown-method` for `unknown_method` matches no branch, so the client
    raises the catch-all and nothing says the label was meant to mean more.
    """
    emitted = {c for _, codes in _envelopes() if codes for c in codes}
    understood = codes_the_client_branches_on(CLIENT.read_text())

    assert understood, "no `code == ...` comparison found in the envelope reader; is this reading?"
    # `internal` is deliberately absent from the client's branches: it is the
    # fall-through, and naming it would be a branch that changes nothing.
    unknown = sorted(emitted - understood - {"internal"})
    assert not unknown, (
        f"the dispatcher emits {unknown} and the client branches on none of them, "
        f"so a reply carrying one is classified as a generic server error. "
        f"The client understands {sorted(understood)}."
    )


# --- controls ---------------------------------------------------------------


def test_CONTROL_the_sweep_reports_an_envelope_with_no_code():
    """Otherwise the assertion above passes on a reader that finds nothing."""
    planted = """
def answer():
    return {"success": False, "error": "boom", "timestamp": "t"}
"""
    found = error_envelopes(planted)

    assert found == [(3, None)], found


def test_CONTROL_the_sweep_reads_a_code_when_there_is_one():
    """The other direction, so "reports None" is not all it can do."""
    planted = """
def answer():
    return {"success": False, "error": "boom", "code": "refused"}
"""
    assert error_envelopes(planted) == [(3, frozenset({"refused"}))], error_envelopes(planted)


def test_CONTROL_a_success_envelope_is_not_swept():
    """The sweep is about ERROR envelopes; a success reply carries no code."""
    planted = """
def answer():
    return {"success": True, "result": 1}
"""
    assert error_envelopes(planted) == []


def test_CONTROL_prose_about_the_field_is_not_read_as_the_field():
    """This repo has been bitten by a guard that matched its own explanation."""
    planted = """
CODE_DOC = "every envelope carries a code, like {\\"success\\": False}"
# {"success": False, "error": "x"} in a comment is not an envelope either
"""
    assert error_envelopes(planted) == []


def test_CONTROL_a_conditional_code_is_read_on_both_branches():
    """The shape that would have made the first version of this guard lie.

    `"internal" if crashed else "refused"` is labelled twice over, not not-at-
    all, and a reader that accepts only `ast.Constant` reports it as missing.
    """
    planted = """
def answer(crashed):
    return {"success": False, "error": "x", "code": "internal" if crashed else "refused"}
"""
    assert error_envelopes(planted) == [(3, frozenset({"internal", "refused"}))]


def test_CONTROL_a_code_that_is_not_a_literal_is_reported_as_unreadable():
    """A computed code cannot be checked against the client's vocabulary."""
    planted = """
def answer(kind):
    return {"success": False, "error": "x", "code": kind}
"""
    assert error_envelopes(planted) == [(3, None)]

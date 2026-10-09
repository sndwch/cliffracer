"""A wire name bound to a variable first is still a wire name.

A reader that only inspects string literals in the call sees
`js.create_key_value(bucket=bucket_name)` and reports nothing, because the
argument is a `Name`. The bucket is a literal one line earlier. Two modules of
the integration tier read as fully converted under a literal-only sweep while
every raw JetStream call in them addressed an unprefixed object.

So this resolves a name to its assignment inside the same function before
deciding. The CONTROL pins the difference: the same source is flagged by this
reader and missed by a literal-only one, which is the only thing that makes the
resolution worth having.

WHAT IT READS, AND WHAT IT DOES NOT. Two shapes: a bare string literal, and a
name bound to one in the same function. It does not read an inline f-string, an
f-string reached through a name, a name returned by any non-helper call, a
`durable=` keyword, or a nested `ConsumerConfig(durable_name=...)`. `RAW_CALLS`
also omits `add_stream`, which is the call that CLAIMS a name rather than
looking one up. The tier contains none of those shapes in a raw call today, so
they are not read: a rule matching nothing is a rule nobody maintains. The claim
this file makes is narrower than its name suggests, and that is the honest
reading of it.

DURABLE NAMES IN RAW CALLS ARE DELIBERATELY NOT PREFIXED. A consumer belongs to
one stream, so two prefixed streams may each carry a durable of the same name
without colliding. A durable the FRAMEWORK created must still be looked up
prefixed, because the container renders it through `ServiceConfig.prefixed_name`;
a durable a test created itself needs only to be spelled the same way at both
ends.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
TIER = REPO / "tests" / "integration"

# Raw broker calls: these bypass ServiceConfig, so nothing prefixes them for you.
RAW_CALLS = frozenset(
    {
        "create_key_value",
        "delete_key_value",
        "delete_stream",
        "stream_info",
        "purge_stream",
        "add_consumer",
        "delete_consumer",
        "consumer_info",
        "pull_subscribe",
    }
)

HELPERS = frozenset(
    {
        "prefixed_name",
        "prefixed_subject",
        "outbound_subject",
        "with_namespace",
        "dlq_subject",
        "effective_event_subject",
        "describe_subject",
    }
)

WIRE_KWARGS = frozenset({"bucket", "stream", "subject", "name"})


def _call_name(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")


def _from_helper(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and _call_name(node) in HELPERS


def _literal_bindings(func: ast.AST) -> dict[str, ast.expr]:
    """Names assigned a bare string literal somewhere in this function."""
    bound: dict[str, ast.expr] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bound[target.id] = node.value
    return bound


def unprefixed_raw_calls(source: str, *, resolve_names: bool) -> list[str]:
    """Raw broker calls whose wire name is not built by a helper.

    With `resolve_names` off this is the literal-only reader that missed them.
    """
    found: list[str] = []
    tree = ast.parse(source)
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        bound = _literal_bindings(func) if resolve_names else {}
        for node in ast.walk(func):
            if not isinstance(node, ast.Call) or _call_name(node) not in RAW_CALLS:
                continue
            args = list(node.args[:1]) + [kw.value for kw in node.keywords if kw.arg in WIRE_KWARGS]
            for arg in args:
                if _from_helper(arg):
                    continue
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append(f"{_call_name(node)}:{node.lineno} literal {arg.value!r}")
                elif resolve_names and isinstance(arg, ast.Name) and arg.id in bound:
                    found.append(f"{_call_name(node)}:{node.lineno} via {arg.id}")
    return found


CONTROL_SOURCE = """
async def uses_a_bound_literal(js):
    bucket_name = "challenge_cron_overlap"
    await js.create_key_value(bucket=bucket_name)

async def uses_a_helper(js):
    bucket_name = prefixed_name("challenge_cron_overlap")
    await js.create_key_value(bucket=prefixed_name("challenge_cron_overlap"))
"""


def test_CONTROL_a_name_bound_to_a_literal_is_found():
    found = unprefixed_raw_calls(CONTROL_SOURCE, resolve_names=True)
    assert any("via bucket_name" in f for f in found), found


def test_CONTROL_a_literal_only_reader_misses_it():
    """The whole reason the resolution exists. If this ever passes, delete it."""
    found = unprefixed_raw_calls(CONTROL_SOURCE, resolve_names=False)
    assert found == [], f"a literal-only reader saw it after all: {found}"


def test_CONTROL_the_reader_reports_exactly_the_bound_literal():
    """An exact set, because the obvious phrasing of this control cannot fail.

    It was written as `not any("uses_a_helper" in f for f in found)`. Entries are
    formatted from the CALLED function's name -- `create_key_value` -- so the
    enclosing function's name never appears in one, and that assertion is false
    for every possible input. Worse, disabling `_from_helper` entirely does not
    change the output: a `Call` argument is neither a `Constant` nor a `Name`,
    so it falls through both branches and is never reported, helper or not. The
    exemption was coming from the fall-through, and nothing would have noticed
    if helper recognition broke.

    An exact set reds in both directions: if the helper line is ever reported,
    and if the bound-literal line stops being.
    """
    found = unprefixed_raw_calls(CONTROL_SOURCE, resolve_names=True)
    assert found == ["create_key_value:4 via bucket_name"], found


def test_no_raw_broker_call_in_the_tier_spells_its_own_wire_name():
    offenders: list[str] = []
    for path in sorted(TIER.glob("*.py")):
        for hit in unprefixed_raw_calls(path.read_text(), resolve_names=True):
            offenders.append(f"{path.name}:{hit}")
    assert not offenders, (
        "a raw broker call addresses a name nothing prefixed; build it through "
        "prefixed_name/prefixed_subject:\n  " + "\n  ".join(offenders)
    )

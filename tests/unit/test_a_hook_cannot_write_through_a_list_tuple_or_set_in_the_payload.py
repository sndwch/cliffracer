"""A send hook's `ctx.payload` is a copy of the caller's containers: dict, list, tuple and set.

`test_send_side_hooks.py` pins the dict case. The module there promises the same for a list, a tuple
and a set, and each is a separate branch of the copy, so each has its own case here: a write through
the context reaches neither the caller's object nor the value the next hook reads from it.
"""

import pytest

from cliffracer.core.dispatch.pipeline import ExtensionPipeline

pytestmark = pytest.mark.unit


def _context(payload):
    return ExtensionPipeline([]).create_send_context(
        kind="event", subject="things.happened", payload=payload, correlation_id="cid"
    )


def test_a_write_to_a_list_in_the_payload_does_not_reach_the_callers_list():
    items = [1, {"k": "v"}]
    ctx = _context({"items": items})

    ctx.payload["items"].append("LEAKED")
    ctx.payload["items"][1]["k"] = "LEAKED"

    assert items == [1, {"k": "v"}], items


def test_a_write_through_a_tuple_in_the_payload_does_not_reach_the_callers_dict():
    inner = {"k": "v"}
    ctx = _context({"pair": (inner, 2)})

    ctx.payload["pair"][0]["k"] = "LEAKED"

    assert inner == {"k": "v"}, inner


def test_a_write_to_a_set_in_the_payload_does_not_reach_the_callers_set():
    tags = {"a", "b"}
    ctx = _context({"tags": tags})

    ctx.payload["tags"].add("LEAKED")

    assert tags == {"a", "b"}, tags


def test_CONTROL_a_hook_reads_each_container_as_the_caller_built_it():
    """A copy that returned nothing, or the wrong type, would satisfy the three tests above."""
    ctx = _context({"items": [1, {"k": "v"}], "pair": ({"k": "v"}, 2), "tags": {"a", "b"}})

    assert ctx.payload["items"] == [1, {"k": "v"}]
    assert isinstance(ctx.payload["pair"], tuple) and ctx.payload["pair"] == ({"k": "v"}, 2)
    assert isinstance(ctx.payload["tags"], set) and ctx.payload["tags"] == {"a", "b"}

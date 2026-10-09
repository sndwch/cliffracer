"""`ensure_streams` refuses the whole declaration before it creates any of it.

It used to create each stream as it went, so when the second of two specs conflicted (an overlap
with another service's stream, or a differing stream already on the broker) the first was already
on the broker, holding its subject claims on a shared broker for a service that never came up, and
the error named only the first conflict, so a service declaring six streams learned of them one
restart at a time. Every spec is now checked against the broker and against the others first; the
conflicts are reported together; nothing is created or updated unless every spec is accepted.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec, ensure_streams

pytestmark = pytest.mark.unit


def _js(existing_specs=()):
    js = AsyncMock()
    entries = [SimpleNamespace(config=spec.to_stream_config()) for spec in existing_specs]

    class _Page:
        total = len(entries)

        def __iter__(self):
            return iter(entries)

    js.streams_info_iterator.return_value = _Page()
    return js


OTHER = StreamSpec(name="OTHERS", subjects=["shared.events.>"])
GOOD = StreamSpec(name="MINE_A", subjects=["mine.a.>"])
CLASHES = StreamSpec(name="MINE_B", subjects=["shared.events.thing"])  # inside OTHERS' claim


async def test_a_conflict_in_a_later_spec_leaves_the_earlier_one_uncreated():
    js = _js([OTHER])

    with pytest.raises(StreamDeclarationError, match="MINE_B"):
        await ensure_streams(js, [GOOD, CLASHES])

    assert js.add_stream.await_count == 0, [
        c.kwargs["config"].name for c in js.add_stream.await_args_list
    ]
    assert js.update_stream.await_count == 0


async def test_every_conflict_is_reported_in_one_error():
    other2 = StreamSpec(name="OTHERS2", subjects=["more.events.>"])
    js = _js([OTHER, other2])
    clash_a = StreamSpec(name="MINE_B", subjects=["shared.events.thing"])
    clash_b = StreamSpec(name="MINE_C", subjects=["more.events.thing"])

    with pytest.raises(StreamDeclarationError) as caught:
        await ensure_streams(js, [GOOD, clash_a, clash_b])

    message = str(caught.value)
    assert "MINE_B" in message and "MINE_C" in message, message
    assert js.add_stream.await_count == 0


async def test_a_differing_existing_stream_refuses_before_a_new_one_is_added():
    differing = StreamSpec(name="MINE_A", subjects=["mine.a.>"], storage="memory")
    js = _js([StreamSpec(name="MINE_A", subjects=["mine.a.>"], storage="file")])

    with pytest.raises(StreamDeclarationError, match="MINE_A"):
        await ensure_streams(js, [StreamSpec(name="NEW", subjects=["new.>"]), differing])

    assert js.add_stream.await_count == 0


async def test_two_specs_that_overlap_each_other_create_neither():
    js = _js()
    a = StreamSpec(name="A", subjects=["x.>"])
    b = StreamSpec(name="B", subjects=["x.y"])

    with pytest.raises(StreamDeclarationError):
        await ensure_streams(js, [a, b])

    assert js.add_stream.await_count == 0


async def test_a_conflict_in_an_update_leaves_a_pending_add_uncreated():
    js = _js([StreamSpec(name="MINE_A", subjects=["mine.a.>"]), OTHER])
    change = StreamSpec(name="MINE_A", subjects=["shared.events.thing"])  # now clashes

    with pytest.raises(StreamDeclarationError):
        await ensure_streams(
            js, [StreamSpec(name="NEW", subjects=["new.>"]), change], allow_update=True
        )

    assert js.add_stream.await_count == 0
    assert js.update_stream.await_count == 0


async def test_CONTROL_with_no_conflict_every_spec_is_still_created_in_order():
    js = _js([OTHER])
    second = StreamSpec(name="MINE_C", subjects=["mine.c.>"])

    await ensure_streams(js, [GOOD, second])

    assert [c.kwargs["config"].name for c in js.add_stream.await_args_list] == ["MINE_A", "MINE_C"]


async def test_CONTROL_a_single_conflict_keeps_its_own_message():
    """One conflict reports as itself: not wrapped in the several-conflicts text."""
    js = _js([OTHER])

    with pytest.raises(StreamDeclarationError) as caught:
        await ensure_streams(js, [CLASHES])

    message = str(caught.value)
    assert message.startswith("stream 'MINE_B' claims 'shared.events.thing'"), message
    assert "OTHERS" in message and "shared.events.thing" in message, message
    # the wrapper that several conflicts get: a count, "none was created", a bulleted list
    assert "cannot be declared" not in message, message
    assert "none was created" not in message, message
    assert "\n  - " not in message, message

"""Declaring a stream is idempotent, and a conflict names the conflict."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec, ensure_streams


def _js(existing_specs=()):
    """A fake JetStreamContext whose streams_info returns the given declarations."""
    js = AsyncMock()
    js.streams_info.return_value = [
        SimpleNamespace(config=spec.to_stream_config()) for spec in existing_specs
    ]
    return js


@pytest.mark.unit
@pytest.mark.asyncio
async def test_absent_stream_is_added():
    js = _js()
    spec = StreamSpec(name="EXTRACTION", subjects=["*.events.extraction.*"])
    await ensure_streams(js, [spec])

    assert js.add_stream.await_count == 1
    assert js.add_stream.call_args.kwargs["config"].name == "EXTRACTION"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_identical_declaration_is_a_no_op():
    """A publisher and its consumer must both be able to declare the shared stream."""
    spec = StreamSpec(name="EXTRACTION", subjects=["*.events.extraction.*"])
    js = _js([spec])
    await ensure_streams(js, [StreamSpec(name="EXTRACTION", subjects=["*.events.extraction.*"])])

    assert js.add_stream.await_count == 0
    assert js.update_stream.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_subject_order_does_not_make_a_declaration_conflict():
    js = _js([StreamSpec(name="X", subjects=["a.b", "a.c"])])
    await ensure_streams(js, [StreamSpec(name="X", subjects=["a.c", "a.b"])])
    assert js.update_stream.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_differing_declaration_raises_by_default():
    js = _js([StreamSpec(name="X", subjects=["a.b"])])
    with pytest.raises(StreamDeclarationError) as exc:
        await ensure_streams(js, [StreamSpec(name="X", subjects=["a.b", "a.c"])])

    message = str(exc.value)
    assert "X" in message
    assert "a.c" in message
    assert "jetstream_update_streams" in message
    assert js.update_stream.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_differing_declaration_updates_when_allowed():
    js = _js([StreamSpec(name="X", subjects=["a.b"])])
    await ensure_streams(js, [StreamSpec(name="X", subjects=["a.b", "a.c"])], allow_update=True)

    assert js.update_stream.await_count == 1
    assert set(js.update_stream.call_args.kwargs["config"].subjects) == {"a.b", "a.c"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overlapping_claim_against_another_stream_raises_naming_both():
    """The jorbo case: a natural-looking jorbo.events.> collides with EXTRACTION."""
    js = _js([StreamSpec(name="EXTRACTION", subjects=["*.events.extraction.*"])])
    with pytest.raises(StreamDeclarationError) as exc:
        await ensure_streams(js, [StreamSpec(name="JORBO", subjects=["jorbo.events.>"])])

    message = str(exc.value)
    assert "JORBO" in message
    assert "EXTRACTION" in message
    assert "jorbo.events.>" in message
    assert "*.events.extraction.*" in message
    assert js.add_stream.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_overlapping_claims_coexist():
    js = _js([StreamSpec(name="EXTRACTION", subjects=["*.events.extraction.*"])])
    await ensure_streams(
        js,
        [
            StreamSpec(name="JORBO_USER", subjects=["jorbo.events.user.*"]),
            StreamSpec(name="JORBO_DLQ", subjects=["jorbo.dlq.*"]),
        ],
    )
    assert js.add_stream.await_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_specs_in_one_call_are_checked_against_each_other():
    js = _js()
    with pytest.raises(StreamDeclarationError):
        await ensure_streams(
            js,
            [
                StreamSpec(name="A", subjects=["x.>"]),
                StreamSpec(name="B", subjects=["x.y"]),
            ],
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_specs_does_not_call_the_server():
    js = _js()
    await ensure_streams(js, [])
    assert js.streams_info.await_count == 0

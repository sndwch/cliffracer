"""Declaring a stream is idempotent, and a conflict names the conflict."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js.api import StreamConfig
from nats.js.errors import NotFoundError

from cliffracer.core.jetstream import (
    StreamDeclarationError,
    StreamSpec,
    ensure_streams,
    validate_bound_streams,
)

pytestmark = pytest.mark.unit


def _wire(spec):
    """What `js.streams_info()` hands back for `spec`'s stream: storage and retention as the plain
    strings the server reports, not the enums `to_stream_config()` builds. Written out here and not
    derived from the spec's own method, so the comparison under test is not fed by its own output."""
    return StreamConfig(
        name=spec.name,
        subjects=list(spec.subjects),
        storage=spec.storage,
        retention=spec.retention,
        max_age=spec.max_age_seconds or 0,
        duplicate_window=spec.duplicate_window_seconds,
    )


def _js(existing_specs=(), *, wire=False):
    """A fake JetStreamContext that lists the given declarations.

    With `wire=True` the listed configs have the shape a real server returns (see `_wire`).

    `ensure_streams` reads the broker through `all_streams`, which asks
    `streams_info_iterator` -- the nats-py call that reports the server's
    `total` alongside the page, and the reason the reader needs no page-size
    constant. A double has to answer the method the code calls, so this one
    returns a page object rather than a bare list.
    """
    js = AsyncMock()
    entries = [
        SimpleNamespace(config=_wire(spec) if wire else spec.to_stream_config())
        for spec in existing_specs
    ]

    class _Page:
        total = len(entries)

        def __iter__(self):
            return iter(entries)

    js.streams_info_iterator.return_value = _Page()
    return js


@pytest.mark.asyncio
async def test_absent_stream_is_added():
    js = _js()
    spec = StreamSpec(name="EXTRACTION", subjects=["jorbo.events.extraction.*"])
    await ensure_streams(js, [spec])

    assert js.add_stream.await_count == 1
    assert js.add_stream.call_args.kwargs["config"].name == "EXTRACTION"


@pytest.mark.asyncio
@pytest.mark.parametrize("retention", ["limits", "interest", "workqueue"])
async def test_the_declared_retention_is_what_add_stream_is_given(retention):
    """The call that decides: what the broker is asked to create, not what the spec reports."""
    js = _js()

    await ensure_streams(js, [StreamSpec(name="EXTRACTION", subjects=["a.>"], retention=retention)])

    assert js.add_stream.call_args.kwargs["config"].retention.value == retention


@pytest.mark.asyncio
async def test_identical_declaration_is_a_no_op():
    """A publisher and its consumer must both be able to declare the shared stream."""
    spec = StreamSpec(name="EXTRACTION", subjects=["jorbo.events.extraction.*"])
    js = _js([spec])
    await ensure_streams(
        js, [StreamSpec(name="EXTRACTION", subjects=["jorbo.events.extraction.*"])]
    )

    assert js.add_stream.await_count == 0
    assert js.update_stream.await_count == 0


@pytest.mark.asyncio
async def test_an_identical_declaration_is_a_no_op_against_a_wire_shaped_stream():
    """The normal case for two services sharing a stream: the stream exists and the server reports
    its storage and retention as plain strings. `ensure_streams` must still see no difference."""
    spec = StreamSpec(
        name="EXTRACTION", subjects=["jorbo.events.extraction.*"], retention="workqueue"
    )
    js = _js([spec], wire=True)

    await ensure_streams(js, [spec])

    assert js.add_stream.await_count == 0
    assert js.update_stream.await_count == 0


@pytest.mark.asyncio
async def test_a_differing_declaration_is_a_conflict_against_a_wire_shaped_stream():
    existing = StreamSpec(name="X", subjects=["a.b"], retention="limits")
    js = _js([existing], wire=True)

    with pytest.raises(StreamDeclarationError, match="X"):
        await ensure_streams(js, [StreamSpec(name="X", subjects=["a.b"], retention="workqueue")])

    assert js.update_stream.await_count == 0


@pytest.mark.asyncio
async def test_subject_order_does_not_make_a_declaration_conflict():
    js = _js([StreamSpec(name="X", subjects=["a.b", "a.c"])])
    await ensure_streams(js, [StreamSpec(name="X", subjects=["a.c", "a.b"])])
    assert js.update_stream.await_count == 0


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


@pytest.mark.asyncio
async def test_differing_declaration_updates_when_allowed():
    js = _js([StreamSpec(name="X", subjects=["a.b"])])
    await ensure_streams(js, [StreamSpec(name="X", subjects=["a.b", "a.c"])], allow_update=True)

    assert js.update_stream.await_count == 1
    assert set(js.update_stream.call_args.kwargs["config"].subjects) == {"a.b", "a.c"}


@pytest.mark.asyncio
async def test_overlapping_claim_against_another_stream_raises_naming_both():
    """The jorbo case: a natural-looking jorbo.events.> collides with EXTRACTION."""
    js = _js([StreamSpec(name="EXTRACTION", subjects=["jorbo.events.extraction.*"])])
    with pytest.raises(StreamDeclarationError) as exc:
        await ensure_streams(js, [StreamSpec(name="JORBO", subjects=["jorbo.events.>"])])

    message = str(exc.value)
    assert "JORBO" in message
    assert "EXTRACTION" in message
    assert "jorbo.events.>" in message
    assert "jorbo.events.extraction.*" in message
    assert js.add_stream.await_count == 0


@pytest.mark.asyncio
async def test_non_overlapping_claims_coexist():
    js = _js([StreamSpec(name="EXTRACTION", subjects=["jorbo.events.extraction.*"])])
    await ensure_streams(
        js,
        [
            StreamSpec(name="JORBO_USER", subjects=["jorbo.events.user.*"]),
            StreamSpec(name="JORBO_DLQ", subjects=["jorbo.dlq.*"]),
        ],
    )
    assert js.add_stream.await_count == 2


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second",
    [
        pytest.param(["c.d"], id="disjoint-subjects"),
        pytest.param(["a.b"], id="identical"),
        pytest.param(["a.>"], id="overlapping"),
    ],
)
async def test_one_stream_name_declared_twice_is_refused_before_anything_is_added(second):
    """A copy-pasted declaration used to add the name twice, and the broker settled it."""
    js = _js()

    with pytest.raises(StreamDeclarationError) as caught:
        await ensure_streams(
            js,
            [
                StreamSpec(name="X", subjects=["a.b"]),
                StreamSpec(name="Y", subjects=["e.f"]),
                StreamSpec(name="X", subjects=second),
            ],
        )

    message = str(caught.value)
    assert "['X']" in message and "'Y'" not in message, message
    assert js.add_stream.await_count == 0
    assert js.streams_info_iterator.await_count == 0


@pytest.mark.asyncio
async def test_every_repeated_name_is_named_not_only_the_first():
    js = _js()

    with pytest.raises(StreamDeclarationError) as caught:
        await ensure_streams(
            js,
            [
                StreamSpec(name="B", subjects=["b.1"]),
                StreamSpec(name="A", subjects=["a.1"]),
                StreamSpec(name="B", subjects=["b.2"]),
                StreamSpec(name="A", subjects=["a.2"]),
            ],
        )

    assert "['A', 'B']" in str(caught.value)


@pytest.mark.asyncio
async def test_CONTROL_distinct_names_with_disjoint_subjects_are_all_added():
    """The refusal is of a repeated NAME, not of declaring several streams in one call."""
    js = _js()

    await ensure_streams(
        js,
        [StreamSpec(name="X", subjects=["a.b"]), StreamSpec(name="Y", subjects=["c.d"])],
    )

    added = [call.kwargs["config"].name for call in js.add_stream.await_args_list]
    assert added == ["X", "Y"]


@pytest.mark.asyncio
async def test_no_specs_does_not_call_the_server():
    js = _js()
    await ensure_streams(js, [])
    assert js.streams_info_iterator.await_count == 0


@pytest.mark.asyncio
async def test_a_preprovisioned_shipment_stream_is_read_by_name_without_listing_or_writing():
    spec = StreamSpec(name="EAST_SHIPMENTS", subjects=["east.shipments.*"])
    js = AsyncMock()
    js.stream_info.return_value = SimpleNamespace(config=spec.to_stream_config())

    await validate_bound_streams(js, [spec])

    js.stream_info.assert_awaited_once_with("EAST_SHIPMENTS")
    js.streams_info_iterator.assert_not_awaited()
    js.add_stream.assert_not_awaited()
    js.update_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_missing_preprovisioned_shipment_stream_names_the_operator_action():
    js = AsyncMock()
    js.stream_info.side_effect = NotFoundError()
    with pytest.raises(StreamDeclarationError) as exc:
        await validate_bound_streams(
            js, [StreamSpec(name="EAST_SHIPMENTS", subjects=["east.shipments.*"])]
        )
    message = str(exc.value)
    assert "EAST_SHIPMENTS" in message
    assert "east.shipments.*" in message
    assert "Create it" in message


@pytest.mark.asyncio
async def test_an_incompatible_preprovisioned_shipment_stream_names_both_contracts():
    js = AsyncMock()
    actual = StreamSpec(name="EAST_SHIPMENTS", subjects=["east.shipments.packed"])
    js.stream_info.return_value = SimpleNamespace(config=actual.to_stream_config())
    with pytest.raises(StreamDeclarationError) as exc:
        await validate_bound_streams(
            js, [StreamSpec(name="EAST_SHIPMENTS", subjects=["east.shipments.*"])]
        )
    message = str(exc.value)
    assert "east.shipments.packed" in message
    assert "east.shipments.*" in message
    assert "Update the provisioned stream" in message


@pytest.mark.asyncio
async def test_a_preprovisioned_shipment_stream_reports_non_subject_drift():
    js = AsyncMock()
    actual = StreamSpec(
        name="EAST_SHIPMENTS",
        subjects=["east.shipments.*"],
        max_age_seconds=30,
        duplicate_window_seconds=20,
    )
    js.stream_info.return_value = SimpleNamespace(config=actual.to_stream_config())
    with pytest.raises(StreamDeclarationError) as exc:
        await validate_bound_streams(
            js,
            [
                StreamSpec(
                    name="EAST_SHIPMENTS",
                    subjects=["east.shipments.*"],
                    max_age_seconds=60,
                    duplicate_window_seconds=50,
                )
            ],
        )
    message = str(exc.value)
    assert "max_age_seconds is 30" in message
    assert "expected 60" in message
    assert "duplicate_window_seconds is 20" in message
    assert "expected 50" in message

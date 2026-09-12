"""With JetStream on, a publish is acked or it raises. There is no quiet middle."""

from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec

pytestmark = pytest.mark.unit


def _svc(**overrides):
    svc = CliffracerService(ServiceConfig(name="jorbo", **overrides))
    svc.nc = AsyncMock()
    svc.js = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_covered_subject_publishes_through_jetstream():
    svc = _svc(
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="JORBO", subjects=["jorbo.events.*"]),
            StreamSpec(name="JORBO_DLQ", subjects=["jorbo.dlq.*"]),
        ],
        namespace="jorbo",
    )
    await svc.publish_event("events.uploaded", upload_id="u1")

    assert svc.js.publish.await_count == 1
    assert svc.nc.publish.await_count == 0
    assert svc.js.publish.call_args.args[0] == "jorbo.events.uploaded"
    assert "correlation_id" in svc.js.publish.call_args.kwargs["headers"]


@pytest.mark.asyncio
async def test_the_store_ack_is_returned_to_the_caller():
    svc = _svc(
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="JORBO", subjects=["jorbo.>"]),
        ],
        namespace="jorbo",
    )
    svc.js.publish.return_value = "puback-sentinel"
    result = await svc.publish_event("events.uploaded", upload_id="u1")
    assert result == "puback-sentinel"


@pytest.mark.asyncio
async def test_uncovered_subject_raises_rather_than_falling_back():
    """A silent fallback to nc.publish makes 'did this get an ack?' invisible."""
    svc = _svc(
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="JORBO_DLQ", subjects=["jorbo.dlq.*"])],
        namespace="jorbo",
    )
    with pytest.raises(StreamDeclarationError) as exc:
        await svc.publish_event("events.user.logged_in", user="u1")

    assert "jorbo.events.user.logged_in" in str(exc.value)
    assert svc.nc.publish.await_count == 0


@pytest.mark.asyncio
async def test_a_failed_store_ack_propagates():
    svc = _svc(
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="JORBO", subjects=["jorbo.>"])],
        namespace="jorbo",
    )
    svc.js.publish.side_effect = TimeoutError("no ack")
    with pytest.raises(TimeoutError):
        await svc.publish_event("events.uploaded", upload_id="u1")

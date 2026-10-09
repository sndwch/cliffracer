"""With JetStream on, a publish is acked or it raises. There is no quiet middle."""

from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import ServiceLifecycleError
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


async def _publish(svc, how):
    if how == "event":
        return await svc.publish_event("events.uploaded", upload_id="u1")
    return await svc.broadcast_message("announce", upload_id="u1")


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["event", "broadcast"])
async def test_jetstream_on_with_no_context_refuses_rather_than_publishing_unacked(how):
    """A service built with JetStream on but not connected used to publish on core NATS."""
    svc = _svc(
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="JORBO", subjects=["jorbo.>"])],
        namespace="jorbo",
    )
    svc.js = None

    with pytest.raises(ServiceLifecycleError) as exc:
        await _publish(svc, how)

    message = str(exc.value)
    assert "'jorbo'" in message and "not connected" in message, message
    assert svc.nc.publish.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["event", "broadcast"])
async def test_CONTROL_jetstream_off_still_publishes_on_core_nats_with_no_context(how):
    """The refusal is of JetStream-on-and-unconnected, not of a publish with no js."""
    svc = _svc(namespace="jorbo")
    svc.js = None

    await _publish(svc, how)

    assert svc.nc.publish.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["event", "broadcast"])
async def test_CONTROL_jetstream_on_and_connected_still_publishes_through_the_stream(how):
    svc = _svc(
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="JORBO", subjects=["jorbo.>"])],
        namespace="jorbo",
    )

    await _publish(svc, how)

    assert svc.js.publish.await_count == 1
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

    # The one attempt went to the stream, and nothing went out on core NATS: a
    # failed store ack must not leave an unacked copy of the event on the wire.
    assert svc.js.publish.await_count == 1
    assert svc.nc.publish.await_count == 0

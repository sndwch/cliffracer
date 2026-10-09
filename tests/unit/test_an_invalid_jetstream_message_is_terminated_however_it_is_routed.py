"""A JetStream delivery that fails validation is terminated whatever routing was chosen.

Whether an invalid message is published to the dead-letter subject is decided by the handler's
`on_invalid` and, where it has none, by `ServiceConfig.default_on_invalid`. The delivery itself is
terminated in every case, including when the dead-letter publish fails, because redelivering a
message that can never validate only repeats the failure. The decision record states both; these
drive a real JetStream dispatch for a typed `@listener` and a `@validated_listener`.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

INVALID = b'{"seq": "not-a-number"}'


class Ping(BaseModel):
    seq: int


def _config(default_on_invalid: str) -> ServiceConfig:
    return ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        default_on_invalid=default_on_invalid,  # type: ignore[arg-type]
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )


def _service(kind: str, on_invalid: str | None, default: str) -> CliffracerService:
    if kind == "typed":

        class Typed(CliffracerService):
            @listener("events.ping", durable="pinger")
            async def on_ping(self, seq: int) -> None:
                raise AssertionError("the handler ran for an invalid message")

        svc: CliffracerService = Typed(_config(default))
    else:

        class Validated(CliffracerService):
            @validated_listener("events.ping", Ping, durable="pinger", on_invalid=on_invalid)
            async def on_ping(self, message: Ping) -> None:
                raise AssertionError("the handler ran for an invalid message")

        svc = Validated(_config(default))
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc._discover_handlers()
    return svc


def _delivery() -> AsyncMock:
    msg = AsyncMock()
    msg.subject = "events.ping"
    msg.data = INVALID
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=1)
    return msg


def _dead_letters(svc: CliffracerService) -> list[str]:
    return [call.args[0] for call in svc.js.publish.call_args_list]


@pytest.mark.parametrize(
    ("kind", "on_invalid", "default", "published"),
    [
        ("typed", None, "deadletter", True),
        ("typed", None, "drop", False),
        ("validated", None, "deadletter", True),
        ("validated", None, "drop", False),
        ("validated", "drop", "deadletter", False),
        ("validated", "deadletter", "drop", True),
    ],
)
async def test_the_routing_chosen_decides_the_publish_and_the_delivery_is_terminated_either_way(
    kind, on_invalid, default, published
):
    svc = _service(kind, on_invalid, default)
    msg = _delivery()

    await svc.container._handle_jetstream_event(msg, pattern="events.ping")

    assert (_dead_letters(svc) == ["dlq.pinger"]) is published, _dead_letters(svc)
    assert msg.term.await_count == 1
    assert msg.ack.await_count == 0 and msg.nak.await_count == 0


@pytest.mark.parametrize("kind", ["typed", "validated"])
async def test_a_failing_dead_letter_publish_still_terminates_and_logs_the_payload(kind):
    svc = _service(kind, None, "deadletter")
    svc.js.publish.side_effect = RuntimeError("stream full")
    msg = _delivery()
    errors: list[str] = []
    sink = logger.add(lambda m: errors.append(m.record["message"]), level="ERROR")
    try:
        await svc.container._handle_jetstream_event(msg, pattern="events.ping")
    finally:
        logger.remove(sink)

    assert msg.term.await_count == 1
    assert msg.ack.await_count == 0 and msg.nak.await_count == 0
    (line,) = [e for e in errors if "Failed to dead-letter" in e]
    assert "stream full" in line and "not-a-number" in line, line

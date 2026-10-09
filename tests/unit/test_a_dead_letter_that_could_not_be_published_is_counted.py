"""A dead letter that could not be published is counted, returned as `False`, and shown on `/health`.

The three dead-letter handlers swallow a failed publish on purpose: the delivery is terminated
either way, because a redelivery would retry the publish only while the server has a delivery
left, and the invalid path must never loop. The failure used to leave one log line and nothing
else, so an operator could not alarm on a DLQ that had stopped accepting messages. Now each handler
returns whether the dead letter was published, the publisher counts the ones that were not, and
`/health` and `/ready` carry that count as `dead_letters_lost` without changing `status`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.dispatch import DeadLetterPublisher
from cliffracer.core.extension import RetryMessage
from cliffracer.core.health_listener import HealthListener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


class _Strict(BaseModel):
    number: int


class _Recorder:
    """Stands in for the publisher's logger: keeps every line, by level."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def __getattr__(self, level: str):
        return lambda text, *a, **k: self.lines.append((level, text))

    def errors(self) -> list[str]:
        return [t for level, t in self.lines if level == "error"]


def _publisher(nc) -> tuple[DeadLetterPublisher, _Recorder]:
    cfg = ServiceConfig(name="orders", dlq_subject="dlq.{service}")
    conn = SimpleNamespace(nc=nc, js=None, jetstream_active=False)
    log = _Recorder()
    return DeadLetterPublisher(cfg, lambda: conn, logger=log), log


def _failing_nc() -> MagicMock:
    nc = MagicMock()
    nc.publish = AsyncMock(side_effect=OSError("stream is full"))
    return nc


def _working_nc() -> MagicMock:
    nc = MagicMock()
    nc.publish = AsyncMock()
    return nc


def _validation_error() -> Exception:
    try:
        _Strict.model_validate({"number": "not a number"})
    except Exception as exc:
        return exc
    raise AssertionError("the payload was meant to be invalid")


def _msg() -> SimpleNamespace:
    return SimpleNamespace(subject="orders.events.x", data=b'{"id": 7}', headers={}, metadata=None)


async def _invalid(dlq: DeadLetterPublisher, on_invalid: str = "deadletter") -> bool:
    return await dlq.handle_invalid_message(
        "orders.strict", {"number": "x"}, _validation_error(), _Strict, on_invalid
    )


async def _decode(dlq: DeadLetterPublisher) -> bool:
    return await dlq.dead_letter_decode_error(_msg(), ValueError("bad"))


async def _terminated(dlq: DeadLetterPublisher) -> bool:
    return await dlq.dead_letter_terminated(_msg(), "boom", 3)


HANDLERS = [("invalid", _invalid), ("decode", _decode), ("terminated", _terminated)]


@pytest.mark.parametrize(("name", "handler"), HANDLERS)
async def test_a_failed_publish_returns_false_and_is_counted(name, handler):
    dlq, _ = _publisher(_failing_nc())

    assert dlq.lost == 0
    assert await handler(dlq) is False, name
    assert dlq.lost == 1, name


@pytest.mark.parametrize(("name", "handler"), HANDLERS)
async def test_a_failed_publish_still_logs_its_cause_and_the_payload(name, handler):
    dlq, log = _publisher(_failing_nc())

    await handler(dlq)

    (line,) = log.errors()
    assert "OSError" in line and "stream is full" in line, f"{name}: {line}"
    assert "Payload:" in line, f"{name}: {line}"


@pytest.mark.parametrize(("name", "handler"), HANDLERS)
async def test_CONTROL_a_published_dead_letter_returns_true_and_is_not_counted(name, handler):
    nc = _working_nc()
    dlq, log = _publisher(nc)

    assert await handler(dlq) is True, name

    nc.publish.assert_awaited_once()
    assert dlq.lost == 0, name
    assert log.errors() == [], name


async def test_CONTROL_an_invalid_message_the_strategy_drops_loses_nothing():
    nc = _working_nc()
    dlq, _ = _publisher(nc)

    assert await _invalid(dlq, on_invalid="drop") is True

    nc.publish.assert_not_awaited()
    assert dlq.lost == 0


async def test_the_count_accumulates_across_handlers():
    dlq, _ = _publisher(_failing_nc())

    for _, handler in HANDLERS:
        await handler(dlq)
    await _decode(dlq)

    assert dlq.lost == 4


async def test_the_count_counts_only_failures():
    nc = _working_nc()
    dlq, _ = _publisher(nc)
    await _decode(dlq)
    nc.publish.side_effect = OSError("stream is full")
    await _decode(dlq)
    nc.publish.side_effect = None
    await _decode(dlq)

    assert dlq.lost == 1


async def _get(service, path: str) -> tuple[int, dict]:
    listener = HealthListener(service, "127.0.0.1", 0)
    await listener.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await writer.drain()
        raw = await reader.read()
        writer.close()
    finally:
        await listener.stop()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body)


@pytest.mark.parametrize("path", ["/health", "/ready"])
async def test_health_carries_the_count_and_it_leaves_status_alone(path):
    svc = CliffracerService(ServiceConfig(name="a"))
    before_code, before = await _get(svc, path)
    assert before["dead_letters_lost"] == 0

    # No broker connection, so the publish fails the way a lost DLQ does.
    published = await svc.container.dispatcher.dlq.dead_letter_decode_error(_msg(), ValueError("x"))
    after_code, after = await _get(svc, path)

    assert published is False
    assert after["dead_letters_lost"] == 1
    assert (after_code, after["status"]) == (before_code, before["status"])


async def test_the_facade_hands_the_result_back():
    svc = CliffracerService(ServiceConfig(name="a"))

    assert await svc.container._dead_letter_decode_error(_msg(), ValueError("x")) is False
    assert await svc.container._dead_letter_terminated(_msg(), "boom", 3) is False
    assert (
        await svc.container.dispatcher._handle_invalid_message(
            "a.strict", {"number": "x"}, _validation_error(), _Strict, "deadletter"
        )
        is False
    )
    assert svc.container.dead_letters_lost == 3


class _Connected:
    """Stands in for nats-py's client: connected and open."""

    is_closed = False
    is_connected = True


async def test_a_running_service_stays_healthy_with_a_lost_dead_letter():
    """`status` is `stopped` for a service that is not running, so this one is, and is healthy."""
    svc = CliffracerService(ServiceConfig(name="a"))
    svc._running = True
    svc.nc = _Connected()

    before_code, before = await _get(svc, "/ready")
    await svc.container.dispatcher.dlq.dead_letter_decode_error(_msg(), ValueError("x"))
    after_code, after = await _get(svc, "/ready")

    assert (before_code, before["status"], before["dead_letters_lost"]) == (200, "healthy", 0)
    assert (after_code, after["status"], after["dead_letters_lost"]) == (200, "healthy", 1)


class _Pinger(CliffracerService):
    def __init__(self, config, error):
        super().__init__(config)
        self._error = error

    @listener("events.ping", durable="pinger")
    async def on_ping(self, subject: str, seq: int = 0):
        raise self._error


def _jetstream_message(num_delivered: int) -> AsyncMock:
    msg = AsyncMock()
    msg.subject = "events.ping"
    msg.data = b'{"seq": 1}'
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    return msg


@pytest.mark.parametrize(
    "error", [RuntimeError("handler exploded"), RetryMessage("not yet", retry_after=1.0)]
)
async def test_a_message_out_of_deliveries_is_still_terminated_when_its_dead_letter_is_lost(error):
    """The decision: terminate on both paths, never nak. A nak would retry the publish only while the
    server has a delivery left, and a message out of deliveries has none."""
    config = ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )
    svc = _Pinger(config, error)
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.js.publish.side_effect = RuntimeError("stream full")
    svc._discover_handlers()
    msg = _jetstream_message(num_delivered=config.jetstream_max_deliver)

    await svc.container._handle_jetstream_event(msg)

    assert msg.term.await_count == 1
    assert msg.nak.await_count == 0
    assert svc.container.dead_letters_lost == 1

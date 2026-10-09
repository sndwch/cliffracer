"""A redelivery is logged where it is decided: which handler, which delivery, why, and how long."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.extension import Extension, RetryMessage
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


def _config(**overrides) -> ServiceConfig:
    options = {
        "jetstream_max_deliver": 4,
        "jetstream_nak_backoff": 0.5,
        "jetstream_max_backoff": 3.0,
    }
    return ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.>"]),
        ],
        **{**options, **overrides},
    )


def _msg(num_delivered: int) -> AsyncMock:
    msg = AsyncMock()
    msg.subject = "events.ping"
    msg.data = b'{"seq": 1}'
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    return msg


class Failing(CliffracerService):
    @listener("events.ping", durable="pinger")
    async def on_ping(self, seq: int) -> None:
        raise RuntimeError("db down")


async def _deliver(service_class, num_delivered: int, **config) -> tuple[AsyncMock, list[str]]:
    service = service_class(_config(**config))
    await service.container._setup_extensions()
    service._discover_handlers()
    service.container.js = AsyncMock()
    msg = _msg(num_delivered)
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        await service.container._handle_jetstream_event(msg, pattern="events.ping")
    finally:
        logger.remove(sink)
    return msg, warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(("delivery", "delay"), [(1, 0.5), (2, 1.0), (3, 2.0)])
async def test_a_handler_failure_that_will_be_retried_is_logged_with_its_delivery_and_delay(
    delivery, delay
):
    msg, warnings = await _deliver(Failing, delivery)

    msg.nak.assert_awaited_once_with(delay=delay)
    (line,) = warnings
    assert "handler on_ping failed on events.ping" in line, line
    assert f"delivery {delivery}/4" in line, line
    assert "RuntimeError: db down" in line, line
    assert f"NAKing with delay={delay:g}s" in line, line


@pytest.mark.asyncio
async def test_the_delay_in_the_line_is_the_delay_the_nak_carried():
    """Read from the config's backoff, so a line that quoted a constant would not match."""
    msg, warnings = await _deliver(
        Failing, 2, jetstream_nak_backoff=0.25, jetstream_max_backoff=9.0
    )

    msg.nak.assert_awaited_once_with(delay=0.5)
    assert "delay=0.5s" in warnings[0], warnings


@pytest.mark.asyncio
async def test_an_extension_that_defers_the_message_is_logged_with_its_reason_and_delay():
    class Defer(Extension):
        async def worker_setup(self, ctx) -> None:
            raise RetryMessage("quota exhausted", retry_after=7.0)

    class Svc(CliffracerService):
        defer = Defer()

        @listener("events.ping", durable="pinger")
        async def on_ping(self, seq: int) -> None:
            pass

    msg, warnings = await _deliver(Svc, 1)

    msg.nak.assert_awaited_once_with(delay=7.0)
    (line,) = warnings
    assert "deferred events.ping (delivery 1/4): quota exhausted; NAKing with delay=7s" in line, (
        line
    )


@pytest.mark.asyncio
async def test_CONTROL_the_delivery_at_the_limit_is_dead_lettered_not_retried():
    msg, warnings = await _deliver(Failing, 4)

    msg.nak.assert_not_awaited()
    msg.term.assert_awaited_once()
    assert not any("NAKing" in line for line in warnings), warnings
    assert any("Dead-lettered" in line for line in warnings), warnings


@pytest.mark.asyncio
async def test_CONTROL_a_handler_that_succeeds_logs_no_warning_and_is_acked():
    class Fine(CliffracerService):
        @listener("events.ping", durable="pinger")
        async def on_ping(self, seq: int) -> None:
            pass

    msg, warnings = await _deliver(Fine, 1)

    msg.ack.assert_awaited_once()
    assert warnings == []


@pytest.mark.asyncio
async def test_a_handlers_own_timeout_error_is_logged_like_any_other_failure():
    """Only an overrun the dispatcher itself reported is left to its own line."""

    class TimesOut(CliffracerService):
        @listener("events.ping", durable="pinger")
        async def on_ping(self, seq: int) -> None:
            raise TimeoutError("the upstream call timed out")

    msg, warnings = await _deliver(TimesOut, 1)

    msg.nak.assert_awaited_once()
    (line,) = warnings
    assert "TimeoutError: the upstream call timed out" in line, line

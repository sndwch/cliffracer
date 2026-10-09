"""The heartbeat is paced by the `ack_wait` the server enforces for the durable, not only the config's.

A durable keeps the `ack_wait` it was created with. When the config now asks for more, the server holds
the shorter one, and the heartbeat pulsed at half the config's value, too slowly for it: every message
handled after the one-time drift warning was redelivered under a running handler, so one message was
handled three times by a 2.5 s handler against a 1 s durable. The drift is read at subscribe, as the
server's `max_deliver` already is, and the heartbeat is paced against the shorter of the two.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.aio.msg import Msg
from nats.js.api import ConsumerConfig

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

PATTERN = "events.order"
OTHER = "events.other"


class Slow(CliffracerService):
    @listener(PATTERN, durable="orders-d")
    async def on_order(self, subject: str, number: int = 0) -> None:
        await asyncio.sleep(0.35)


def _config(**extra) -> ServiceConfig:
    return ServiceConfig(
        name="orders",
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
        **extra,
    )


def _sub(ack_wait):
    """A pull or push subscription whose durable reports `ack_wait` (seconds) to `consumer_info`."""
    sub = MagicMock()
    sub.consumer_info = AsyncMock(
        return_value=SimpleNamespace(
            config=ConsumerConfig(durable_name="orders-d", ack_wait=ack_wait, max_deliver=5),
            stream_name="EVENTS",
        )
    )
    return sub


def _dispatcher(**config):
    svc = Slow(_config(**config))
    return svc, svc.container.dispatcher.jetstream


async def test_the_interval_is_half_the_shorter_of_the_config_and_the_server():
    _, js = _dispatcher(jetstream_ack_wait=30.0)
    assert js._pulse_interval(PATTERN) == 15.0, "nothing is known about the server yet"

    await js.report_consumer_drift(_sub(1.0), "orders-d", pattern=PATTERN)

    assert js._pulse_interval(PATTERN) == 0.5
    assert js._pulse_interval(OTHER) == 15.0, "another durable is paced by the config"
    assert js._pulse_interval() == 15.0


async def test_a_server_ack_wait_longer_than_the_configs_does_not_slow_the_heartbeat():
    _, js = _dispatcher(jetstream_ack_wait=10.0)

    await js.report_consumer_drift(_sub(120.0), "orders-d", pattern=PATTERN)

    assert js._pulse_interval(PATTERN) == 5.0


async def test_a_reading_that_fails_drops_the_one_before_it():
    _, js = _dispatcher(jetstream_ack_wait=30.0)
    await js.report_consumer_drift(_sub(1.0), "orders-d", pattern=PATTERN)
    failing = MagicMock()
    failing.consumer_info = AsyncMock(side_effect=RuntimeError("the consumer is gone"))

    await js.report_consumer_drift(failing, "orders-d", pattern=PATTERN)

    assert js._pulse_interval(PATTERN) == 15.0


@pytest.mark.parametrize("unusable", [0, -1.0, None, True, "1.0"])
async def test_a_server_ack_wait_that_is_not_a_positive_number_is_ignored(unusable):
    _, js = _dispatcher(jetstream_ack_wait=30.0)
    sub = MagicMock()
    config = ConsumerConfig(durable_name="orders-d", max_deliver=5)
    object.__setattr__(config, "ack_wait", unusable)
    sub.consumer_info = AsyncMock(return_value=SimpleNamespace(config=config, stream_name="EVENTS"))

    await js.report_consumer_drift(sub, "orders-d", pattern=PATTERN)

    assert js._pulse_interval(PATTERN) == 15.0


async def test_the_floor_of_the_interval_holds_for_a_tiny_server_ack_wait():
    _, js = _dispatcher(jetstream_ack_wait=30.0)

    await js.report_consumer_drift(_sub(0.01), "orders-d", pattern=PATTERN)

    assert js._pulse_interval(PATTERN) == 0.05


def _delivery():
    client = MagicMock()
    client.publish = AsyncMock()
    msg = Msg(
        _client=client,
        subject=PATTERN,
        reply="$JS.ACK.EVENTS.orders-d.1.1.1.1700000000000000000.0",
        data=json.dumps({"number": 1}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return msg, client


def _pulses(client) -> int:
    bodies = [c.args[1] if len(c.args) > 1 else b"+ACK" for c in client.publish.await_args_list]
    return sum(1 for body in bodies if body.startswith(b"+WPI"))


async def _handle(server_ack_wait: float | None):
    svc, js = _dispatcher(jetstream_ack_wait=30.0)
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    if server_ack_wait is not None:
        await js.report_consumer_drift(_sub(server_ack_wait), "orders-d", pattern=PATTERN)
    msg, client = _delivery()
    await asyncio.wait_for(svc.container._handle_jetstream_event(msg, pattern=PATTERN), timeout=5)
    return client


async def test_a_handler_longer_than_the_servers_ack_wait_is_pulsed_in_time():
    client = await _handle(server_ack_wait=0.2)

    assert _pulses(client) >= 2, "the server would have redelivered the message under the handler"


async def test_CONTROL_with_nothing_known_about_the_server_the_config_paces_the_heartbeat():
    client = await _handle(server_ack_wait=None)

    assert _pulses(client) == 0


async def test_the_permit_wait_is_paced_by_the_same_interval():
    svc, js = _dispatcher(jetstream_ack_wait=30.0, max_event_concurrency=1)
    await js.report_consumer_drift(_sub(0.2), "orders-d", pattern=PATTERN)
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    first, _ = _delivery()
    second, second_client = _delivery()
    running = asyncio.create_task(js._bounded_handle_jetstream_event(first, PATTERN))
    await asyncio.sleep(0.05)
    waiting = asyncio.create_task(js._bounded_handle_jetstream_event(second, PATTERN))

    await asyncio.wait_for(running, timeout=5)
    pulses_while_it_waited = _pulses(second_client)
    await asyncio.wait_for(waiting, timeout=5)

    assert pulses_while_it_waited >= 2, "a message waiting for a permit was not pulsed in time"

"""The dead-letter decision is taken against the limit the server enforces.

A durable consumer keeps the `max_deliver` it was created with; a later
subscribe adopts it. A service whose `jetstream_max_deliver` is higher than the
durable's therefore NAKs the server's last delivery, the server stops
redelivering, and the message is never dead-lettered or logged.

The limit is `min(server, config)`, read once per durable from the
`consumer_info()` call subscribe already makes -- no extra round-trip per
message. With no reading (no durable, an unreadable consumer, a direct dispatch)
the config decides, which is the behaviour every existing deployment has. The
dead-letter record and log line say which limit decided, so an operator does
not go looking in the wrong config.

These drive the container's real subscribe sites, push and pull, so the reading
is shown to reach the dispatch path it decides, not only to be stored.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from nats.js.api import AckPolicy, ConsumerConfig

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

SHAPES = pytest.mark.parametrize("pull", [False, True], ids=["push", "pull"])


def _config(max_deliver: int) -> ServiceConfig:
    return ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
        jetstream_max_deliver=max_deliver,
    )


def _info(max_deliver: int) -> SimpleNamespace:
    return SimpleNamespace(
        stream_name="EVENTS",
        config=ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=30.0,
            max_deliver=max_deliver,
            max_ack_pending=64,
        ),
    )


async def _subscribed(pull: bool, config_limit: int, consumer_info) -> tuple:
    """A failing durable listener, subscribed through the container.

    `consumer_info` is the subscription's `consumer_info` return value, an
    exception for it to raise, or None to leave the mock's own return value.
    """

    class S(CliffracerService):
        @listener("events.ping", durable="pinger", pull=pull)
        async def on_ping(self, subject: str, seq: int = 1) -> None:
            raise RuntimeError("handler exploded")

    svc = S(_config(config_limit))
    svc._discover_handlers()
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    sub = AsyncMock()
    if isinstance(consumer_info, Exception):
        sub.consumer_info.side_effect = consumer_info
    elif consumer_info is not None:
        sub.consumer_info.return_value = consumer_info
    if pull:
        svc.js.pull_subscribe.return_value = sub
    else:
        svc.js.subscribe.return_value = sub

    await svc.container._setup_subscriptions()
    await asyncio.sleep(0)  # let the pull loop see the service is not running
    return svc, sub


def _pattern(svc) -> str:
    (pattern,) = svc.container.registry.event_handlers
    return pattern


async def _deliver(svc, sub, pull: bool, num_delivered: int):
    """Deliver one message the way the subscription would, and wait for its outcome."""
    msg = AsyncMock()
    msg.subject = _pattern(svc)
    msg.data = b'{"seq": 1}'
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)

    if pull:
        sub.fetch = AsyncMock(return_value=[msg])
        await svc.container.dispatcher.pull_once(sub, pattern=_pattern(svc))
    else:
        callback = svc.js.subscribe.await_args.kwargs["cb"]
        await callback(msg)
        for _ in range(200):
            if msg.term.await_count or msg.nak.await_count:
                break
            await asyncio.sleep(0.005)
    return msg


def _dead_letters(svc) -> list[dict]:
    return [
        json.loads(call.args[1])
        for call in svc.js.publish.await_args_list
        if call.args[0].startswith("dlq.") or ".dlq." in call.args[0]
    ]


def _outcome(msg) -> str:
    return f"term={msg.term.await_count} nak={msg.nak.await_count} ack={msg.ack.await_count}"


@SHAPES
@pytest.mark.asyncio
async def test_a_lower_server_limit_dead_letters_at_the_servers_last_delivery(pull):
    svc, sub = await _subscribed(pull, config_limit=5, consumer_info=_info(3))

    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        msg = await _deliver(svc, sub, pull, num_delivered=3)
    finally:
        logger.remove(sink)

    assert (msg.term.await_count, msg.nak.await_count) == (1, 0), _outcome(msg)
    (record,) = _dead_letters(svc)
    assert record["deliveries"] == 3
    assert record["delivery_limit"] == "server max_deliver 3"
    (logged,) = [w for w in warnings if w.startswith("Dead-lettered")]
    assert "after 3 deliveries (server max_deliver 3)" in logged, logged


@SHAPES
@pytest.mark.asyncio
async def test_a_lower_server_limit_does_not_terminate_early(pull):
    """The control: below the server's limit the message is still retried."""
    svc, sub = await _subscribed(pull, config_limit=5, consumer_info=_info(3))

    msg = await _deliver(svc, sub, pull, num_delivered=2)

    assert (msg.term.await_count, msg.nak.await_count) == (0, 1), _outcome(msg)
    assert _dead_letters(svc) == []


@SHAPES
@pytest.mark.asyncio
async def test_a_higher_server_limit_leaves_the_config_deciding(pull):
    """Terminating at the config's limit stops the server's redeliveries, as asked."""
    svc, sub = await _subscribed(pull, config_limit=3, consumer_info=_info(5))

    msg = await _deliver(svc, sub, pull, num_delivered=3)

    assert (msg.term.await_count, msg.nak.await_count) == (1, 0), _outcome(msg)
    (record,) = _dead_letters(svc)
    assert record["delivery_limit"] == "config jetstream_max_deliver 3"


@SHAPES
@pytest.mark.asyncio
async def test_an_unlimited_server_leaves_the_config_deciding(pull):
    svc, sub = await _subscribed(pull, config_limit=5, consumer_info=_info(-1))

    early = await _deliver(svc, sub, pull, num_delivered=4)
    last = await _deliver(svc, sub, pull, num_delivered=5)

    assert (early.term.await_count, early.nak.await_count) == (0, 1), _outcome(early)
    assert (last.term.await_count, last.nak.await_count) == (1, 0), _outcome(last)
    (record,) = _dead_letters(svc)
    assert record["delivery_limit"] == "config jetstream_max_deliver 5"


@SHAPES
@pytest.mark.asyncio
async def test_an_unreadable_consumer_falls_back_to_the_config_and_says_so(pull):
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        svc, sub = await _subscribed(
            pull, config_limit=5, consumer_info=RuntimeError("broker said no")
        )
    finally:
        logger.remove(sink)

    unverified = [w for w in warnings if "could not read" in w]
    assert len(unverified) == 1, warnings
    assert "jetstream_max_deliver 5" in unverified[0]
    assert "broker said no" in unverified[0]

    msg = await _deliver(svc, sub, pull, num_delivered=5)
    assert msg.term.await_count == 1, _outcome(msg)
    (record,) = _dead_letters(svc)
    assert record["delivery_limit"] == "config jetstream_max_deliver 5"


@SHAPES
@pytest.mark.asyncio
async def test_a_reading_that_is_not_a_number_is_not_a_limit(pull):
    """A mock subscription answers with a MagicMock, as many tests here do.

    Comparing that against the config would raise inside the dispatch's own
    exception handler, so the message would be neither naked nor terminated.
    """
    svc, sub = await _subscribed(pull, config_limit=5, consumer_info=None)

    early = await _deliver(svc, sub, pull, num_delivered=3)
    last = await _deliver(svc, sub, pull, num_delivered=5)

    assert (early.term.await_count, early.nak.await_count) == (0, 1), _outcome(early)
    assert (last.term.await_count, last.nak.await_count) == (1, 0), _outcome(last)


@pytest.mark.asyncio
async def test_a_failed_reread_does_not_keep_the_previous_reading():
    """A resubscribe whose read fails must not dead-letter on a durable that may be gone."""
    svc, sub = await _subscribed(False, config_limit=5, consumer_info=_info(3))
    sub.consumer_info.side_effect = RuntimeError("broker said no")

    await svc.container._setup_subscriptions()
    msg = await _deliver(svc, sub, False, num_delivered=3)

    assert (msg.term.await_count, msg.nak.await_count) == (0, 1), _outcome(msg)


@pytest.mark.asyncio
async def test_a_dispatch_with_no_reading_uses_the_config():
    """The harness and any direct dispatch have no subscription to read."""
    svc, sub = await _subscribed(False, config_limit=5, consumer_info=_info(3))
    svc.container.dispatcher.jetstream._server_max_deliver.clear()

    msg = await _deliver(svc, sub, False, num_delivered=5)

    assert msg.term.await_count == 1, _outcome(msg)
    (record,) = _dead_letters(svc)
    assert record["delivery_limit"] == "config jetstream_max_deliver 5"


@pytest.mark.asyncio
async def test_the_drift_warning_says_where_messages_will_be_dead_lettered():
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        await _subscribed(False, config_limit=5, consumer_info=_info(3))
    finally:
        logger.remove(sink)

    (drift,) = [w for w in warnings if "runs with" in w]
    assert "max_deliver=3, not the 5 asked for" in drift
    assert "dead-lettered after delivery 3, the server's limit" in drift

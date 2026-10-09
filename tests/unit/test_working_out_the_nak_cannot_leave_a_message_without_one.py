"""A failed delivery is NAKed whatever the delay is worked out from.

Three computations run in the failure branch before the NAK is sent, and each could raise or send
nothing, leaving the message until `ack_wait` instead of handing it back. `nak_delay` raised
`OverflowError` once `num_delivered` passed 1024, because an integer power past 2**1023 does not
convert to a float (`jetstream_max_deliver` has no upper bound). A `RetryMessage` with a `retry_after`
of `nan` or `inf` passed the `<= 0` test and failed to encode in nats-py, where `safe_nak` swallowed it
and nothing fell back to a plain NAK. A message with no JetStream reply is read through
`message_metadata` since the dead-letter fix; the same holds here.

The deliveries are real `nats.aio.msg.Msg` objects with an ack subject, and what each one published
there is read back.
"""

import asyncio
import json
import math
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.aio.msg import Msg

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.extension import RetryMessage
from cliffracer.core.jetstream import StreamSpec, nak_delay

pytestmark = pytest.mark.unit

MODE: dict = {}


class Failing(CliffracerService):
    @listener("events.order", durable="orders-d")
    async def on_order(self, subject: str, number: int = 0) -> None:
        if MODE["kind"] == "retry":
            raise RetryMessage("busy", retry_after=MODE["retry_after"])
        raise RuntimeError("boom")


def _config(**extra) -> ServiceConfig:
    return ServiceConfig(
        name="orders",
        health_port=0,
        jetstream_enabled=True,
        jetstream_max_deliver=10**6,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
        **extra,
    )


def _delivery(num_delivered: int = 1, *, reply: str | None = None):
    client = MagicMock()
    client.publish = AsyncMock()
    msg = Msg(
        _client=client,
        subject="events.order",
        reply=(
            f"$JS.ACK.EVENTS.orders-d.{num_delivered}.1.1.1700000000000000000.0"
            if reply is None
            else reply
        ),
        data=json.dumps({"number": 1}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return msg, client


async def _handle(kind: str, num_delivered: int, retry_after=None, **config):
    svc = Failing(_config(**config))
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    MODE.update(kind=kind, retry_after=retry_after)
    msg, client = _delivery(num_delivered)
    await asyncio.wait_for(
        svc.container._handle_jetstream_event(msg, pattern="events.order"), timeout=5
    )
    return [call.args[1] for call in client.publish.await_args_list]


def _delays(published: list[bytes]) -> list[float | None]:
    out: list[float | None] = []
    for body in published:
        if body.startswith(b"-NAK"):
            rest = body[len(b"-NAK") :].strip()
            out.append(json.loads(rest)["delay"] / 1e9 if rest else None)
    return out


@pytest.mark.parametrize("delivered", [1023, 1024, 1025, 5000, 10**5])
async def test_a_failing_delivery_is_nacked_however_often_it_has_been_delivered(delivered):
    published = await _handle("raise", delivered, jetstream_max_backoff=60.0)

    assert _delays(published) == [60.0 if delivered > 6 else pytest.approx(2 ** (delivered - 1))]


@pytest.mark.parametrize("delivered", [1, 2, 3, 6])
async def test_CONTROL_the_backoff_still_doubles_below_the_cap(delivered):
    published = await _handle("raise", delivered, jetstream_max_backoff=60.0)

    assert _delays(published) == [pytest.approx(min(2 ** (delivered - 1), 60.0))]


@pytest.mark.parametrize(
    "retry_after",
    [
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="inf"),
        pytest.param(-math.inf, id="minus-inf"),
        pytest.param(-3.0, id="negative"),
        pytest.param(0, id="zero"),
        pytest.param(None, id="none"),
        pytest.param("soon", id="not-a-number"),
        pytest.param(True, id="a-bool"),
    ],
)
async def test_a_retry_after_that_is_no_usable_delay_falls_back_to_the_configured_backoff(
    retry_after,
):
    published = await _handle("retry", 3, retry_after, jetstream_nak_backoff=1.0)

    assert _delays(published) == [pytest.approx(4.0)]


async def test_CONTROL_a_usable_retry_after_is_the_delay_that_is_sent():
    published = await _handle("retry", 3, 2.5)

    assert _delays(published) == [pytest.approx(2.5)]


def test_nak_delay_is_capped_for_any_delivery_count():
    config = _config(jetstream_nak_backoff=1.0, jetstream_max_backoff=45.0)

    assert [nak_delay(n, config) for n in (1, 2, 3, 7, 1025, 10**9)] == [1.0, 2.0, 4.0, 45.0] + [
        45.0,
        45.0,
    ]


async def test_a_delayed_nak_that_cannot_be_sent_falls_back_to_a_plain_nak():
    svc = Failing(_config())
    msg = AsyncMock()
    msg.subject = "events.order"
    msg.nak.side_effect = [ValueError("cannot encode the delay"), None]

    sent = await svc.container.dispatcher.jetstream.safe_nak(msg, delay=5.0)

    assert sent is True
    assert [call.kwargs for call in msg.nak.await_args_list] == [{"delay": 5.0}, {}]


async def test_a_nak_that_fails_both_ways_reports_false_and_does_not_raise():
    svc = Failing(_config())
    msg = AsyncMock()
    msg.subject = "events.order"
    msg.nak.side_effect = ValueError("the connection is gone")

    assert await svc.container.dispatcher.jetstream.safe_nak(msg, delay=5.0) is False


async def test_a_nak_with_no_delay_that_fails_is_attempted_once():
    """With no delay the delayed and the plain form are one call: a failure is not about the delay."""
    svc = Failing(_config())
    msg = AsyncMock()
    msg.subject = "events.order"
    msg.nak.side_effect = OSError("network unreachable")

    assert await svc.container.dispatcher.jetstream.safe_nak(msg) is False
    msg.nak.assert_awaited_once_with(delay=0.0)


async def test_CONTROL_a_nak_that_works_is_sent_once_with_its_delay():
    svc = Failing(_config())
    msg = AsyncMock()
    msg.subject = "events.order"

    assert await svc.container.dispatcher.jetstream.safe_nak(msg, delay=5.0) is True
    msg.nak.assert_awaited_once_with(delay=5.0)


async def test_a_message_with_no_jetstream_reply_is_handled_without_raising():
    svc = Failing(_config())
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    MODE.update(kind="raise", retry_after=None)
    msg, client = _delivery(reply="")

    await asyncio.wait_for(
        svc.container._handle_jetstream_event(msg, pattern="events.order"), timeout=5
    )

    assert client.publish.await_args_list == []


@pytest.mark.parametrize("retry_after", [0.5, 2])
async def test_a_retry_after_above_zero_is_the_delay_sent(retry_after):
    """A `retry_after` is used when it is a finite number above zero, a fraction of a second
    included; it is not replaced by the configured backoff."""
    published = await _handle("retry", 1, retry_after=retry_after)

    assert _delays(published) == [pytest.approx(float(retry_after))]

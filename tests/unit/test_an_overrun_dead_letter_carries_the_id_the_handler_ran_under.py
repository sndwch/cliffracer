"""The dead letter for a handler that overran `max_processing_time` carries the id the handler ran under.

An exception a handler raises is stamped with the dispatch's id, and the dead letter reads the id back
from it. A handler cancelled at its budget raises nothing the dispatch can stamp: the overrun is
re-raised as a new `TimeoutError`, which carried no id, so the dead letter fell back to the wire and
minted a new id for a message that arrived with none. The id appeared in none of the handler's log
lines, and the overrun dead letters, which an operator most needs to trace, did not join the logs.

The deliveries are real `nats.aio.msg.Msg` objects with an ack subject.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.aio.msg import Msg

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

BUDGET = 0.2
SEEN: dict[str, str | None] = {}


class Behaving(CliffracerService):
    @listener("events.order", durable="orders-d")
    async def on_order(self, subject: str, number: int = 0) -> None:
        SEEN["handler"] = CorrelationContext.get()
        mode = SEEN.get("mode")
        if mode == "raise":
            raise RuntimeError("boom")
        if mode == "wedge":
            await asyncio.Event().wait()
        if mode == "suppress":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return  # the handler swallows the cancellation and returns


def _config() -> ServiceConfig:
    return ServiceConfig(
        name="orders",
        health_port=0,
        jetstream_enabled=True,
        jetstream_max_deliver=3,
        max_processing_time=BUDGET,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )


def _delivery(*, headers: dict | None = None, payload: dict | None = None, num_delivered=3):
    client = MagicMock()
    client.publish = AsyncMock()
    msg = Msg(
        _client=client,
        subject="events.order",
        reply=f"$JS.ACK.EVENTS.orders-d.{num_delivered}.1.1.1700000000000000000.0",
        data=json.dumps(payload or {"number": 1}).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    return msg, client


async def _run(mode: str, **delivery_kwargs):
    svc = Behaving(_config())
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    SEEN.clear()
    SEEN["mode"] = mode
    msg, client = _delivery(**delivery_kwargs)
    await asyncio.wait_for(
        svc.container._handle_jetstream_event(msg, pattern="events.order"), timeout=5
    )
    (call,) = svc.js.publish.await_args_list
    record = json.loads(call.args[1])
    return SEEN["handler"], record, call.kwargs["headers"], client


@pytest.mark.parametrize("mode", ["wedge", "suppress"])
async def test_an_overrun_dead_letter_carries_the_id_the_handler_ran_under(mode):
    ran_under, record, headers, _ = await _run(mode)

    assert ran_under, "the handler ran under an id"
    assert record["correlation_id"] == ran_under
    assert headers["correlation_id"] == ran_under
    assert "max_processing_time" in record["error"]


@pytest.mark.parametrize("mode", ["wedge", "suppress"])
async def test_an_overrun_dead_letter_for_a_message_with_an_id_on_the_wire_keeps_it(mode):
    ran_under, record, headers, _ = await _run(mode, headers={"X-Correlation-ID": "from-the-wire"})

    assert ran_under == "from-the-wire"
    assert record["correlation_id"] == headers["correlation_id"] == "from-the-wire"


async def test_an_overrun_dead_letter_for_a_message_with_an_id_in_the_payload_keeps_it():
    ran_under, record, _, _ = await _run(
        "wedge", payload={"number": 1, "correlation_id": "from-the-payload"}
    )

    assert ran_under == "from-the-payload"
    assert record["correlation_id"] == "from-the-payload"


async def test_CONTROL_a_handler_that_raises_is_held_to_the_same_rule():
    ran_under, record, headers, _ = await _run("raise")

    assert ran_under and record["correlation_id"] == headers["correlation_id"] == ran_under
    assert record["error"] == "RuntimeError", (
        "the handler's text is withheld unless the flag lets it out"
    )


async def test_an_overrun_is_still_terminated_and_counted_as_one():
    ran_under, record, _, client = await _run("wedge")

    published = [call.args[1] for call in client.publish.await_args_list]
    assert any(body.startswith(b"+TERM") for body in published), published
    assert ran_under == record["correlation_id"]

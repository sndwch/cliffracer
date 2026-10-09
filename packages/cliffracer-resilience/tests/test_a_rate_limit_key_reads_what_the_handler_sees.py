"""A rate-limit key is resolved against what the handler receives.

For an event, the message on the wire is an envelope: the payload the handler
is called with is under `data`, and the top level describes the publish. A
string key used to be looked up at the top level, so it found nothing for an
event and fell back to its own name -- every user in one bucket called
"user_id", one user's traffic refusing everyone's. A callable key was handed
the whole envelope, so a callable reading a payload field failed the same way.

Both kinds of key now resolve against `data` when the message is an event
envelope, and against the whole payload otherwise. The callable is given a copy
of the context whose `payload` is `data`; the context every other extension
shares is not changed.

Each case goes through the real extension and the real event dispatch, with
two users and a limit of one call each: a working per-user key handles both, a
collapsed one refuses the second.
"""

from typing import Literal
from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import ResilienceExtension, rate_limit

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.extension import Extension
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

SUBJECT = "orders.placed"
USERS = ("u-1", "u-2")


def _users_counted(buckets: set[str]) -> set[str]:
    """The key's value in each bucket: a bucket is `<service>:<handler>:<value>`."""
    assert all(bucket.startswith("receiver:on_order:") for bucket in buckets), buckets
    return {bucket.removeprefix("receiver:on_order:") for bucket in buckets}


PUBLISHERS = pytest.mark.parametrize("publisher", ["publish_event", "broadcast_message"])


def _key_from_context(ctx) -> str:
    return ctx.payload["user_id"]


def _key_from_payload(payload: dict) -> str:
    return payload["user_id"]


class _PayloadRecorder(Extension):
    """What every other extension on the chain sees as the payload."""

    name = "payload_recorder"

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def worker_result(self, ctx, result, exc) -> None:
        self.payloads.append(ctx.payload)


async def _deliver(
    publisher: str,
    key,
    *,
    key_source: Literal["header", "payload", "context"] | None = None,
) -> tuple[list[str], set[str], list[dict]]:
    handled: list[str] = []

    class Receiver(CliffracerService):
        resilience = ResilienceExtension()

        @listener(SUBJECT, fanout=True)
        @rate_limit(
            calls=1,
            window=60.0,
            key=key,
            key_source=key_source,
        )
        async def on_order(self, user_id: str, n: int) -> None:
            handled.append(user_id)

    receiver = Receiver(ServiceConfig(name="receiver"))
    recorder = receiver.add_extension(_PayloadRecorder())
    await receiver.container._setup_extensions()
    receiver._discover_handlers()
    receiver.nc = AsyncMock()

    sender = CliffracerService(ServiceConfig(name="sender"))
    sender.nc = AsyncMock()
    for user in USERS:
        await getattr(sender, publisher)(SUBJECT, user_id=user, n=1)
        call = sender.nc.publish.await_args
        msg = MockMessage(subject=SUBJECT, data=call.args[1], headers=dict(call.kwargs["headers"]))
        await receiver.container.dispatcher.events.handle_event(msg, pattern=SUBJECT)

    buckets = set(receiver.resilience.limiter._windows)
    return handled, buckets, recorder.payloads


@PUBLISHERS
@pytest.mark.asyncio
async def test_a_string_key_limits_each_user_separately(publisher):
    handled, buckets, _ = await _deliver(publisher, "user_id", key_source="payload")

    assert _users_counted(buckets) == set(USERS), buckets
    assert handled == list(USERS), handled


@PUBLISHERS
@pytest.mark.asyncio
async def test_a_callable_key_on_the_context_reads_the_producers_data(publisher):
    handled, buckets, _ = await _deliver(publisher, _key_from_context)

    assert _users_counted(buckets) == set(USERS), buckets
    assert handled == list(USERS), handled


@PUBLISHERS
@pytest.mark.asyncio
async def test_a_callable_key_on_the_payload_reads_the_producers_data(publisher):
    handled, buckets, _ = await _deliver(publisher, _key_from_payload, key_source="payload")

    assert _users_counted(buckets) == set(USERS), buckets
    assert handled == list(USERS), handled


@pytest.mark.asyncio
async def test_other_extensions_still_see_the_envelope():
    """The callable's view of the payload is its own; the shared context is untouched."""
    _, _, payloads = await _deliver("broadcast_message", _key_from_context)

    assert len(payloads) == len(USERS), payloads
    for payload in payloads:
        assert set(payload) >= {"data", "source_service", "timestamp"}, payload
        assert "user_id" not in payload, payload

"""Two publishes to one subject in one decorated call are two messages.

`@idempotent` set one ambient key around the whole handler, and every publish
read it, so two sends to the same subject carried a byte-identical
`Nats-Msg-Id`. JetStream dropped the second inside its dedup window and
answered with a normal ack carrying the FIRST message's sequence -- so a
handler emitting `started` then `completed` lost `completed`, and the caller
could not tell.

WHY AN ORDINAL AND NOT THE PAYLOAD. Folding the payload hash in would separate
messages that differ and merge messages that do not, and the issue's own
example is one event per line item -- which can legitimately carry identical
payloads. The ordinal separates them without asking what they contain.

WHY THAT STILL DEDUPLICATES A RETRY, which is the whole point of the feature: a
retried handler re-runs from the start and publishes the same messages in the
same order, so message n of the second attempt is given the id message n of the
first attempt had. That rests on the handler publishing deterministically --
the assumption the single ambient key already made, now written down.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from cliffracer.core.idempotency import (
    IdempotencyContext,
    format_nats_msg_id,
    idempotency_sequence_var,
    idempotent,
)

pytestmark = pytest.mark.unit

SUBJECT = "orders.order.processed"
OTHER = "orders.order.audited"


def _msg_id(subject: str = SUBJECT) -> str:
    """Exactly what `publish_event` puts on the wire for the ambient key."""
    return format_nats_msg_id(
        subject, str(IdempotencyContext.get()), sequence=IdempotencyContext.next_sequence()
    )


@idempotent(key="order_id")
async def two_publishes(order_id: str) -> list[str]:
    return [_msg_id(), _msg_id()]


@idempotent(key="order_id")
async def one_publish(order_id: str) -> list[str]:
    return [_msg_id()]


@idempotent(key="order_id")
def two_publishes_sync(order_id: str) -> list[str]:
    return [_msg_id(), _msg_id()]


async def test_two_publishes_to_one_subject_are_two_ids():
    """The defect: these were byte-identical and the second was swallowed."""
    ids = await two_publishes(order_id="order-1")

    assert len(set(ids)) == 2, ids


async def test_a_retry_of_the_handler_reproduces_the_same_ids():
    """The property the ordinal must not break, and the reason for the feature.

    A retry re-runs the handler from the start, so message n gets the id
    message n had, and JetStream recognises both as duplicates. An ordinal that
    did not reset per call -- a process-wide counter, say -- would pass the
    test above and fail this one.
    """
    first = await two_publishes(order_id="order-1")
    second = await two_publishes(order_id="order-1")

    assert first == second, (first, second)


async def test_a_different_key_shares_no_ids():
    first = await two_publishes(order_id="order-1")
    other = await two_publishes(order_id="order-2")

    assert set(first).isdisjoint(other)


async def test_the_first_message_keeps_the_id_it_had():
    """A handler that publishes once is not re-keyed by this change.

    Message 0 carries no suffix, so in-flight deduplication for the common case
    -- one publish per decorated call -- is not reset by deploying this.
    """
    ids = await one_publish(order_id="order-1")

    assert ids == [f"{SUBJECT}:order-1"], ids


async def test_the_ordinal_counts_messages_not_subjects():
    """Two subjects in one call still get distinct ids, and stay distinct on retry."""

    @idempotent(key="order_id")
    async def two_subjects(order_id: str) -> list[str]:
        return [_msg_id(SUBJECT), _msg_id(OTHER)]

    first = await two_subjects(order_id="order-1")
    assert len(set(first)) == 2, first
    assert first == await two_subjects(order_id="order-1")


def test_a_sync_handler_gets_the_same_treatment():
    """The sync wrapper collided identically and was missed on the first pass."""
    ids = two_publishes_sync(order_id="order-1")

    assert len(set(ids)) == 2, ids
    assert ids == two_publishes_sync(order_id="order-1")


async def test_concurrent_calls_do_not_share_a_counter():
    """Each invocation counts its own messages.

    A counter held anywhere but the call's own context would interleave between
    concurrent handlers, and two callers would take ordinals from one another --
    which breaks the retry property for both.
    """
    results = await asyncio.gather(
        two_publishes(order_id="order-1"),
        two_publishes(order_id="order-2"),
        two_publishes(order_id="order-1"),
    )

    assert results[0] == results[2], "the same key twice must give the same ids"
    assert set(results[0]).isdisjoint(results[1])


def test_outside_a_decorated_call_there_is_no_ordinal():
    """A caller keying its own publish gets the id it chose, unnumbered."""
    assert idempotency_sequence_var.get() is None
    assert IdempotencyContext.next_sequence() is None
    assert format_nats_msg_id(SUBJECT, "mine", sequence=None) == f"{SUBJECT}:mine"


async def test_the_counter_is_cleared_when_the_handler_returns():
    """Otherwise the next call in this context would start mid-sequence."""
    await two_publishes(order_id="order-1")

    assert idempotency_sequence_var.get() is None


# --- the real publish path, not a reconstruction of it ----------------------


async def _ids_from_real_publishes(order_id: str) -> list[str]:
    """Run a decorated handler through `publish_event` and capture the headers.

    Every test above composes the id the way `publish_event` does, which tests
    `format_nats_msg_id` and the counter but NOT the line that joins them.
    Removing `sequence=` from `service.py` left all of them green -- the
    wiring was the one part with no test on it. This drives the real method
    and reads the header it actually sets.
    """
    from cliffracer import CliffracerService, ServiceConfig

    service = CliffracerService(ServiceConfig(name="orders", health_listener=False))
    service.nc = AsyncMock()  # a send needs a connection; the publish below is stubbed, not made
    captured: list[str] = []

    async def capture(full_subject: str, data: bytes, content_type: str, headers: dict) -> None:
        captured.append(headers.get("Nats-Msg-Id"))

    async def run_hooks(ctx, send):
        return await send()

    service._publish_serialized = capture  # type: ignore[method-assign]
    service.container.dispatcher._run_send_hooks = run_hooks  # type: ignore[method-assign]

    @idempotent(key="order_id")
    async def handler(order_id: str) -> None:
        await service.publish_event("order.processed", stage="started", order_id=order_id)
        await service.publish_event("order.processed", stage="completed", order_id=order_id)

    await handler(order_id=order_id)
    return captured


async def test_publish_event_puts_a_distinct_id_on_each_message():
    ids = await _ids_from_real_publishes("order-1")

    assert len(ids) == 2, ids
    assert len(set(ids)) == 2, ids


async def test_publish_event_reproduces_the_ids_on_a_retry():
    first = await _ids_from_real_publishes("order-1")
    second = await _ids_from_real_publishes("order-1")

    assert first == second, (first, second)


# --- the ordinal survives the length cap ------------------------------------


def test_a_long_subject_and_key_still_give_each_message_its_own_id():
    """The ordinal goes into the hash's input, not onto its output.

    `format_nats_msg_id` hashes anything over 128 bytes to bound the header. If
    the ordinal were appended after that check, every message of a call would
    hash the same string and JetStream would keep only the first and silently
    drop the rest, for exactly the deployments most likely to hit it.

    Reachable rather than theoretical: a `compute_payload_hash` key is 64 hex
    characters, so any subject over about 63 bytes crosses the threshold, and a
    prefixed multi-tenant subject gets there. The property was stated in a
    comment and tested by nothing, which is what this covers.
    """
    subject = "tenant.acme.region.euw1.orders.order.processed"
    key = "k" * 105
    assert len(f"{subject}:{key}") > 128, "the premise: this must cross the hashing threshold"

    ids = [format_nats_msg_id(subject, key, sequence=n) for n in (0, 1, 2)]

    assert len(set(ids)) == 3, ids


def test_CONTROL_a_long_id_is_still_bounded():
    """The cap it must not defeat: the header stays a hash, not a long string."""
    subject = "tenant.acme.region.euw1.orders.order.processed"
    key = "k" * 105

    for sequence in (0, 1, 2):
        assert len(format_nats_msg_id(subject, key, sequence=sequence)) == 64

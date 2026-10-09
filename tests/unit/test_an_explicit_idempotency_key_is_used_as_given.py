"""A key the caller passes to `publish_event` is the message's `Nats-Msg-Id` exactly, ordinal or not.

Inside an `@idempotent` call every publish took the call's next ordinal, a caller's explicit
`idempotency_key=` included, so the id a key produced depended on how many publishes came before it. The
documented remedy for a handler that publishes from concurrent tasks (an explicit key per publish)
therefore still depended on publish order. An explicit key is now used as given and does not advance
the count; the ordinal belongs to the key `@idempotent` derives.
"""

import hashlib

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.idempotency import idempotent
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit


class Jobs(CliffracerService):
    @rpc
    @idempotent(key="job_id")
    async def two_explicit(self, job_id: str, order: str) -> str:
        first, second = ("a", "b") if order == "ab" else ("b", "a")
        await self.publish_event("jobs.done", idempotency_key=f"part-{first}", part=first)
        await self.publish_event("jobs.done", idempotency_key=f"part-{second}", part=second)
        return "ok"

    @rpc
    @idempotent(key="job_id")
    async def the_same_explicit_key_twice(self, job_id: str) -> str:
        await self.publish_event("jobs.done", idempotency_key="once", part="1")
        await self.publish_event("jobs.done", idempotency_key="once", part="2")
        return "ok"

    @rpc
    @idempotent(key="job_id")
    async def explicit_between_derived(self, job_id: str) -> str:
        await self.publish_event("jobs.done", part="d0")
        await self.publish_event("jobs.done", idempotency_key="x", part="e0")
        await self.publish_event("jobs.done", part="d1")
        await self.publish_event("jobs.done", idempotency_key="y", part="e1")
        await self.publish_event("jobs.done", part="d2")
        return "ok"

    @rpc
    @idempotent(key="job_id")
    async def two_derived(self, job_id: str) -> str:
        await self.publish_event("jobs.done", part="d0")
        await self.publish_event("jobs.done", part="d1")
        return "ok"

    @rpc
    @idempotent(key="job_id")
    async def an_empty_key_is_no_key(self, job_id: str) -> str:
        await self.publish_event("jobs.done", idempotency_key="", part="0")
        await self.publish_event("jobs.done", idempotency_key="", part="1")
        return "ok"

    @rpc
    @idempotent(key="job_id")
    async def a_long_explicit_key(self, job_id: str) -> str:
        await self.publish_event("jobs.done", idempotency_key="k" * 200, part="long")
        return "ok"

    async def outside_a_decorated_call(self) -> None:
        await self.publish_event("jobs.done", idempotency_key="part-a", part="a")
        await self.publish_event("jobs.done", idempotency_key="part-b", part="b")


def _config() -> ServiceConfig:
    return ServiceConfig(
        name="jobs",
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="JOBS", subjects=["jobs.>"]),
            StreamSpec(name="DLQ", subjects=["dlq.>"]),
        ],
    )


async def _ids(method: str, **payload) -> list[str]:
    async with ServiceTestHarness(Jobs, config=_config()) as harness:
        await harness.rpc(method, job_id="j1", **payload)
        return [headers["Nats-Msg-Id"] for _, _, headers in harness.jetstream.published]


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        ("ab", ["jobs.done:part-a", "jobs.done:part-b"]),
        ("ba", ["jobs.done:part-b", "jobs.done:part-a"]),
    ],
)
async def test_an_explicit_key_gets_the_same_id_whatever_the_publish_order(order, expected):
    assert await _ids("two_explicit", order=order) == expected


async def test_the_same_explicit_key_twice_is_the_same_id_so_the_second_deduplicates():
    assert await _ids("the_same_explicit_key_twice") == ["jobs.done:once", "jobs.done:once"]


async def test_an_explicit_publish_does_not_advance_the_count_of_the_derived_ones():
    assert await _ids("explicit_between_derived") == [
        "jobs.done:j1",
        "jobs.done:x",
        "jobs.done:j1#1",
        "jobs.done:y",
        "jobs.done:j1#2",
    ]


async def test_CONTROL_derived_publishes_still_carry_their_ordinals():
    assert await _ids("two_derived") == ["jobs.done:j1", "jobs.done:j1#1"]


async def test_CONTROL_an_empty_key_is_not_an_explicit_one_and_the_derived_key_numbers_the_messages():
    assert await _ids("an_empty_key_is_no_key") == ["jobs.done:j1", "jobs.done:j1#1"]


async def test_CONTROL_outside_a_decorated_call_an_explicit_key_is_the_id():
    async with ServiceTestHarness(Jobs, config=_config()) as harness:
        await harness.service.outside_a_decorated_call()
        ids = [headers["Nats-Msg-Id"] for _, _, headers in harness.jetstream.published]

    assert ids == ["jobs.done:part-a", "jobs.done:part-b"]


async def test_CONTROL_a_long_explicit_key_is_still_hashed_as_before():
    (msg_id,) = await _ids("a_long_explicit_key")

    assert msg_id == f"jobs.done:{hashlib.sha256(('k' * 200).encode()).hexdigest()}"

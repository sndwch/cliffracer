"""A handler that streams its reply sends each item as it yields it, then one envelope.

An `@rpc` handler written as an async generator, its return annotated `AsyncIterator[X]`, answers a
request carrying `Cliffracer-Stream: 1` with one chunk per item (the item's dump, headed
`Cliffracer-Stream-Seq`) and then the usual envelope with `"items"` and `Cliffracer-Stream-End`.
The stream runs inside the request's own call, so its deadline, its permits and its admission slot
bound the whole of it. It ends early, with the envelope that says why, at a bad item, a raise, a
refusal, the deadline or a stream limit, and silently when the caller has gone.

These run the service on the in-memory broker with a client that subscribes its own inbox, as a
streaming caller does.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.dispatch.rpc_stream import NOT_RUN_ASYNC
from cliffracer.core.extension import RejectMessage
from cliffracer.core.messages import Message
from cliffracer.introspect import describe
from cliffracer.testing import InMemoryBroker, ServiceTestHarness

pytestmark = pytest.mark.unit

SEQ, END, STREAM = "Cliffracer-Stream-Seq", "Cliffracer-Stream-End", "Cliffracer-Stream"
#: Bound on any wait here, so a stream that never ends fails by name instead of hanging.
WITHIN = 5.0


def keeping(handler: Any) -> Any:
    """Keep a reference to each generator the handler builds, on the service, so nothing but an
    explicit close can close it: dropped, a generator is closed by asyncio's finaliser."""

    @functools.wraps(handler)
    def wrapper(service: Any, *args: Any, **kwargs: Any) -> Any:
        generator = handler(service, *args, **kwargs)
        service.kept.append(generator)
        return generator

    return wrapper


class Logs(CliffracerService):
    """Each handler records the items it yielded and whether its generator was closed."""

    def __init__(self, **config: Any) -> None:
        super().__init__(ServiceConfig(name="logs", subject_prefix=None, health_port=0, **config))
        self.yielded: list[Any] = []
        self.closed = False
        self.started = False
        self.release = asyncio.Event()
        self.kept: list[Any] = []

    async def _items(self, values: list[Any], pause: float = 0.0) -> AsyncIterator[Any]:
        self.started = True
        try:
            for value in values:
                self.yielded.append(value)
                yield value
                await asyncio.sleep(pause)
        finally:
            self.closed = True

    @rpc
    async def tail(self, n: int) -> AsyncIterator[int]:
        async for value in self._items(list(range(n))):
            yield value

    @rpc
    async def slow(self, n: int) -> AsyncIterator[int]:
        async for value in self._items(list(range(n)), pause=0.02):
            yield value

    @rpc
    async def wrong(self) -> AsyncIterator[int]:
        async for value in self._items([0, 1, "not a number"]):
            yield value

    @rpc
    async def raising(self) -> AsyncIterator[int]:
        async for value in self._items([0, 1]):
            yield value
        raise RuntimeError("the source failed")

    @rpc
    async def refusing(self) -> AsyncIterator[int]:
        async for value in self._items([0]):
            yield value
        raise RejectMessage("not for you")

    @rpc
    async def held(self) -> AsyncIterator[int]:
        self.started = True
        yield 0
        await self.release.wait()
        yield 1

    @rpc(max_concurrency=1, max_queued=0)
    async def solo(self) -> AsyncIterator[int]:
        self.started = True
        yield 0
        await self.release.wait()
        yield 1

    @rpc
    @keeping
    async def kept_alive(self, n: int) -> AsyncIterator[int]:
        try:
            for value in range(n):
                yield value
                await asyncio.sleep(0.01)
        finally:
            self.closed = True

    @rpc
    async def one(self) -> int:
        return 1


@contextlib.asynccontextmanager
async def running(**config: Any) -> AsyncIterator[tuple[Logs, InMemoryBroker, Any]]:
    broker = InMemoryBroker()
    service = Logs(**config)
    async with ServiceTestHarness(service, broker=broker):
        client = await broker.connect()
        try:
            yield service, broker, client
        finally:
            await client.close()


async def ask(
    client: Any,
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    stream: bool = True,
    headers: dict[str, str] | None = None,
    leave_after: int | None = None,
) -> list[Any]:
    """Send one request from `client`'s own inbox and return what arrives there, up to the
    message that ends it (one with no `Cliffracer-Stream-Seq`), or, with `leave_after`, until that
    many chunks have come, when it unsubscribes."""
    inbox = f"_INBOX.{uuid.uuid4().hex}"
    got: list[Any] = []

    async def record(msg: Any) -> None:
        got.append(msg)

    sub = await client.subscribe(inbox, cb=record)
    await client.flush()
    sent = {"Content-Type": "application/json", **(headers or {})}
    if stream:
        sent[STREAM] = "1"
    await client.publish(
        f"logs.rpc.{method}", json.dumps(payload or {}).encode(), reply=inbox, headers=sent
    )
    deadline = time.monotonic() + WITHIN
    while time.monotonic() < deadline:
        if leave_after is not None and len(got) >= leave_after:
            await sub.unsubscribe()
            return got
        if got and SEQ not in (got[-1].headers or {}):
            return got
        await asyncio.sleep(0.002)
    raise AssertionError(f"{method}: no message ended the stream within {WITHIN}s: {got!r}")


def chunks(got: list[Any]) -> list[tuple[str, Any]]:
    return [(m.headers[SEQ], json.loads(m.data)) for m in got if SEQ in (m.headers or {})]


def ending(got: list[Any]) -> tuple[dict[str, Any], dict[str, str]]:
    last = got[-1]
    return json.loads(last.data), dict(last.headers or {})


async def test_each_item_is_a_chunk_then_one_envelope_ends_the_stream():
    async with running() as (_, _, client):
        got = await ask(client, "tail", {"n": 3})

    assert chunks(got) == [("0", 0), ("1", 1), ("2", 2)]
    envelope, headers = ending(got)
    assert (envelope["success"], envelope["result"], envelope["items"]) == (True, None, 3)
    assert headers[END] == "3"


async def test_a_chunk_carries_its_own_headers_and_not_the_requests():
    async with running() as (_, _, client):
        got = await ask(client, "tail", {"n": 1}, headers={"X-Mine": "1"})

    assert set(got[0].headers) == {"Content-Type", SEQ, "X-Correlation-ID"}, got[0].headers


@pytest.mark.parametrize(
    ("method", "items", "code"),
    [
        pytest.param("wrong", 2, "internal", id="an-item-of-the-wrong-type"),
        pytest.param("raising", 2, "internal", id="a-raise-after-two-items"),
        pytest.param("refusing", 1, "refused", id="a-refusal-after-one-item"),
    ],
)
async def test_a_stream_that_fails_ends_with_its_error_after_the_items_sent(method, items, code):
    async with running() as (service, _, client):
        got = await ask(client, method)
        closed = service.closed

    assert len(chunks(got)) == items
    envelope, headers = ending(got)
    assert (envelope["success"], envelope["code"], envelope["items"]) == (False, code, items)
    assert headers[END] == str(items)
    assert closed, "the handler's generator was not closed"


async def test_a_stream_past_its_deadline_ends_with_deadline_exceeded_after_the_items_sent():
    async with running() as (service, _, client):
        got = await ask(client, "slow", {"n": 1000}, headers={"Cliffracer-Timeout-Ms": "150"})
        closed = service.closed

    sent = len(chunks(got))
    envelope, headers = ending(got)
    assert 0 < sent < 1000
    assert (envelope["code"], envelope["items"], headers[END]) == (
        "deadline_exceeded",
        sent,
        str(sent),
    )
    assert closed


async def test_a_stream_holds_its_permit_until_it_ends():
    async with running(max_rpc_concurrency=1) as (service, _, client):
        streaming = asyncio.create_task(ask(client, "held"))
        await asyncio.wait_for(_until(lambda: service.started), WITHIN)
        unary = asyncio.create_task(ask(client, "one", stream=False))
        await asyncio.sleep(0.1)
        waited = not unary.done()
        service.release.set()
        await asyncio.wait_for(asyncio.gather(streaming, unary), WITHIN)

    assert waited, "a call ran while the stream held the only permit"
    assert json.loads(unary.result()[-1].data)["result"] == 1


async def test_a_stream_holds_its_methods_admission_until_it_ends():
    """A stream is one admitted request of its method for its whole life: with one running and
    none waiting, another call of the method is refused `busy` until the stream ends."""
    async with running() as (service, _, client):
        streaming = asyncio.create_task(ask(client, "solo"))
        await asyncio.wait_for(_until(lambda: service.started), WITHIN)
        refused = await ask(client, "solo")
        service.release.set()
        first = await asyncio.wait_for(streaming, WITHIN)
        service.release.clear()
        service.started = False
        again = asyncio.create_task(ask(client, "solo"))
        await asyncio.wait_for(_until(lambda: service.started), WITHIN)
        service.release.set()
        second = await asyncio.wait_for(again, WITHIN)

    assert json.loads(refused[-1].data)["code"] == "busy"
    assert [json.loads(m.data)["success"] for m in (first[-1], second[-1])] == [True, True]


@pytest.mark.parametrize(
    ("config", "limit"),
    [
        pytest.param({"max_stream_items": 2}, {"items": 2}, id="items"),
        pytest.param({"max_stream_bytes": 2}, {"bytes": 2}, id="bytes"),
    ],
)
async def test_a_stream_at_its_limit_ends_refused_after_the_items_allowed(config, limit):
    async with running(**config) as (service, _, client):
        got = await ask(client, "tail", {"n": 5})
        closed = service.closed

    assert chunks(got) == [("0", 0), ("1", 1)]
    envelope, headers = ending(got)
    assert (envelope["code"], envelope["limit"], envelope["items"], headers[END]) == (
        "refused",
        limit,
        2,
        "2",
    )
    assert closed


@pytest.mark.parametrize(
    ("method", "stream"),
    [
        pytest.param("tail", False, id="a-stream-asked-for-once"),
        pytest.param("one", True, id="a-single-reply-asked-for-as-a-stream"),
    ],
)
async def test_a_request_that_does_not_match_the_handler_is_refused(method, stream):
    async with running() as (service, _, client):
        got = await ask(client, method, {"n": 1} if method == "tail" else {}, stream=stream)
        started = service.started

    envelope = json.loads(got[-1].data)
    assert (envelope["code"], envelope["details"][0]["type"]) == (
        "validation_failed",
        "stream_mismatch",
    )
    assert not started


async def test_the_fire_and_forget_subject_of_a_streaming_handler_runs_nothing_and_says_so():
    records: list[tuple[str, str]] = []
    sink = logger.add(lambda m: records.append((m.record["level"].name, m.record["message"])))
    try:
        async with running() as (service, broker, client):
            await client.publish("logs.async.tail", json.dumps({"n": 3}).encode())
            await broker.settle()
            started = service.started
    finally:
        logger.remove(sink)

    assert not started
    # Called, a generator runs nothing until iterated, so `started` alone holds with no guard.
    assert ("WARNING", NOT_RUN_ASYNC.format("tail")) in records, records


async def test_a_stream_stops_soon_after_its_caller_has_gone_and_sends_no_end():
    async with running() as (service, broker, client):
        got = await ask(client, "slow", {"n": 1000}, leave_after=2)
        await asyncio.wait_for(_until(lambda: service.closed), WITHIN)
        await broker.settle()
        yielded = len(service.yielded)
        published = broker.published

    assert len(got) == 2
    assert yielded < 20, f"the handler went on for {yielded} items after its caller left"
    inbox = next(p.reply for p in published if p.subject == "logs.rpc.slow")
    ends = [p for p in published if p.subject == inbox and END in (p.headers or {})]
    assert ends == [], "an end was sent to a caller that had gone"


@pytest.mark.parametrize(
    ("config", "leave_after"),
    [
        pytest.param({}, 2, id="the-caller-has-gone"),
        pytest.param({"max_stream_items": 2}, None, id="a-limit-is-reached"),
    ],
)
async def test_a_stream_that_ends_early_closes_a_generator_something_else_holds(
    config, leave_after
):
    async with running(**config) as (service, _, client):
        await ask(client, "kept_alive", {"n": 1000}, leave_after=leave_after)
        await asyncio.wait_for(_until(lambda: service.closed), WITHIN)

    assert len(service.kept) == 1


async def test_a_503_heard_after_its_stream_has_ended_leaves_nothing_behind():
    async with running() as (service, broker, client):
        await ask(client, "slow", {"n": 1000}, leave_after=2)
        await asyncio.wait_for(_until(lambda: service.closed), WITHIN)
        await broker.settle()
        back = next(p.reply for p in broker.published if SEQ in (p.headers or {}))
        await client.publish(back, b"", headers={"Status": "503"})
        await broker.settle()
        gone = set(service.container.rpc_dispatcher._streams._gone)

    assert gone == set()


async def test_describe_publishes_a_streaming_handler_as_a_stream_of_its_item():
    methods = {m.name: m.returns for m in describe(Logs).methods}

    assert methods["tail"] == {"kind": "stream", "item": {"kind": "scalar", "name": "int"}}


async def test_the_503_the_broker_sends_is_not_recorded_as_a_publish():
    broker = InMemoryBroker()
    client = await broker.connect()
    await client.subscribe("_INBOX.back", cb=_ignore)
    await client.flush()

    await client.publish("nobody.holds.this", b"x", reply="_INBOX.back")
    await broker.settle()
    await client.close()

    assert [p.subject for p in broker.published] == ["nobody.holds.this"]


async def _ignore(_msg: Any) -> None:
    return None


async def _until(condition: Any) -> None:
    while not condition():
        await asyncio.sleep(0.002)


# --- what an item carries, and what on the back subject means the caller has gone -----------


class Entry(Message):
    """A streamed item that is a `Message`, so the request's correlation id is filled into it."""

    line: int


class Entries(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="entries", subject_prefix=None, health_port=0))

    @rpc
    async def entries(self) -> AsyncIterator[Entry]:
        yield Entry(line=0)
        yield Entry(line=1, correlation_id="its-own")


async def test_a_streamed_item_carries_its_requests_correlation_id_unless_it_has_its_own():
    broker = InMemoryBroker()
    async with ServiceTestHarness(Entries(), broker=broker):
        client = await broker.connect()
        try:
            inbox = f"_INBOX.{uuid.uuid4().hex}"
            got: list[Any] = []

            async def record(msg: Any) -> None:
                got.append(msg)

            await client.subscribe(inbox, cb=record)
            await client.flush()
            await client.publish(
                "entries.rpc.entries",
                b"{}",
                reply=inbox,
                headers={
                    "Content-Type": "application/json",
                    STREAM: "1",
                    "X-Correlation-ID": "the-request",
                },
            )
            await asyncio.wait_for(
                _until(lambda: got and SEQ not in (got[-1].headers or {})), WITHIN
            )
        finally:
            await client.close()

    items = [json.loads(m.data) for m in got if SEQ in (m.headers or {})]
    assert [(item["line"], item["correlation_id"]) for item in items] == [
        (0, "the-request"),
        (1, "its-own"),
    ]


async def test_only_a_503_on_the_back_subject_means_the_caller_has_gone():
    """A stray reply, or a status that is not 503, on a stream's back subject leaves it running."""
    async with running() as (service, _, client):
        inbox = f"_INBOX.{uuid.uuid4().hex}"
        got: list[Any] = []

        async def record(msg: Any) -> None:
            got.append(msg)

        await client.subscribe(inbox, cb=record)
        await client.flush()
        await client.publish(
            "logs.rpc.slow",
            json.dumps({"n": 30}).encode(),
            reply=inbox,
            headers={"Content-Type": "application/json", STREAM: "1"},
        )
        await asyncio.wait_for(_until(lambda: len(got) >= 2), WITHIN)
        back = got[0].reply
        await client.publish(back, b"a stray reply")
        await client.publish(back, b"", headers={"Status": "404"})
        await client.flush()
        try:
            await asyncio.wait_for(
                _until(lambda: got and SEQ not in (got[-1].headers or {})), WITHIN
            )
        except TimeoutError:
            raise AssertionError(
                f"the stream stopped after {len(chunks(got))} items when a message that was not "
                f"a 503 reached its back subject"
            ) from None

    envelope, headers = ending(got)
    assert (envelope["success"], headers[END]) == (True, "30")
    assert len(service.yielded) == 30

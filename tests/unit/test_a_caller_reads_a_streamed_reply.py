"""A caller reads a streamed reply: each item as it arrives, then what the end says.

A generated client's streaming method, `CliffracerService.stream_rpc` and `RpcProxy.stream` all
read through one reader: it yields each item validated against its declared type, raises an
error the stream ends with after the items before it (carrying `items`), raises
`RpcStreamGapError` for an item that never arrived, bounds the whole stream by one timeout, and
unsubscribes its inbox however it ends, so the service stops soon after.

These run the service on the in-memory broker; a fake service, a plain subscriber that answers
with chunks it writes itself, sends what a real one never would.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel

from cliffracer import (
    CliffracerService,
    RpcNoRespondersError,
    RpcProxy,
    RpcRefusedError,
    RpcServerError,
    RpcStreamGapError,
    RpcTimeoutError,
    RpcValidationError,
    ServiceClient,
    ServiceConfig,
    rpc,
)
from cliffracer.core import deadline as deadlines
from cliffracer.core.deadline import Deadline
from cliffracer.core.exceptions import ClientOutOfDateError
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.generate_client import emit
from cliffracer.introspect import describe
from cliffracer.testing import InMemoryBroker, ServiceTestHarness

pytestmark = pytest.mark.unit

SEQ, END = "Cliffracer-Stream-Seq", "Cliffracer-Stream-End"
#: Bound on any wait here, so a stream that never ends fails by name instead of hanging.
WITHIN = 5.0


class Line(BaseModel):
    number: int
    text: str


class Logs(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="logs", subject_prefix=None, health_port=0))
        self.yielded = 0
        self.closed = asyncio.Event()

    @rpc
    async def tail(self, n: int, pause: float = 0.0) -> AsyncIterator[Line]:
        try:
            for number in range(n):
                self.yielded += 1
                yield Line(number=number, text=f"line {number}")
                await asyncio.sleep(pause)
        finally:
            self.closed.set()

    @rpc
    async def raising(self) -> AsyncIterator[int]:
        yield 0
        yield 1
        raise RuntimeError("the source failed")

    @rpc
    async def refusing(self) -> AsyncIterator[int]:
        yield 0
        raise RejectMessage("not for you")

    @rpc
    async def one(self) -> int:
        return 1


def recorder() -> tuple[Extension, list[str], list[tuple[str, Any]]]:
    """An extension that records each send hook's call by kind, into lists it closes over: a
    class-declared extension is copied per service, and the copy appends to these same lists."""
    before: list[str] = []
    after: list[tuple[str, Any]] = []

    class Calls(Extension):
        async def before_call(self, ctx: Any) -> None:
            before.append(ctx.kind)

        async def after_call(self, ctx: Any, result: Any, exc: BaseException | None) -> None:
            after.append((ctx.kind, result))

    return Calls(), before, after


class Reader(CliffracerService):
    """Another service, calling `logs` from its own handlers."""

    logs = RpcProxy("logs")

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="reader", subject_prefix=None, health_port=0))

    @rpc
    async def numbers(self, n: int) -> list[int]:
        return [line["number"] async for line in self.stream_rpc("logs", "tail", n=n)]


def _client_class(service: type[CliffracerService]) -> type[ServiceClient]:
    namespace: dict[str, Any] = {}
    exec(compile(emit(describe(service)), "<generated>", "exec"), namespace)  # noqa: S102
    return next(
        v
        for v in namespace.values()
        if isinstance(v, type) and issubclass(v, ServiceClient) and v is not ServiceClient
    )


@contextlib.asynccontextmanager
async def serving(**client: Any) -> AsyncIterator[tuple[Logs, InMemoryBroker, Any]]:
    broker = InMemoryBroker()
    service = Logs()
    async with ServiceTestHarness(service, broker=broker):
        nc = await broker.connect()
        generated = _client_class(Logs)(nc=nc, **client)
        try:
            yield service, broker, generated
        finally:
            await nc.close()


async def _collect(items: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in items]


async def test_a_generated_client_yields_each_item_in_order_and_typed():
    async with serving(timeout=7.0) as (_, broker, client):
        lines = await asyncio.wait_for(_collect(client.tail(n=3)), WITHIN)

    assert lines == [Line(number=i, text=f"line {i}") for i in range(3)]
    assert all(type(line) is Line for line in lines)
    sent = next(p for p in broker.published if p.subject == "logs.rpc.tail")
    assert sent.headers is not None
    assert (sent.headers["Cliffracer-Stream"], sent.headers["Cliffracer-Timeout-Ms"]) == (
        "1",
        "7000",
    )


@pytest.mark.parametrize(
    ("method", "error", "items"),
    [
        pytest.param("raising", RpcServerError, 2, id="a-raise-after-two-items"),
        pytest.param("refusing", RpcRefusedError, 1, id="a-refusal-after-one-item"),
    ],
)
async def test_an_error_the_stream_ends_with_is_raised_after_the_items_before_it(
    method, error, items
):
    got: list[Any] = []
    async with serving() as (_, _, client):
        with pytest.raises(error) as raised:
            async for item in getattr(client, method)():
                got.append(item)

    assert got == list(range(items))
    assert raised.value.items == items


async def test_leaving_the_loop_early_stops_the_service_soon_after():
    async with serving() as (service, _, client):
        got = []
        async for line in client.tail(n=1000, pause=0.005):
            got.append(line)
            if len(got) == 2:
                break
        await asyncio.wait_for(service.closed.wait(), WITHIN)

    assert service.yielded < 50, f"the handler went on for {service.yielded} items"


async def test_a_call_nothing_answers_raises_no_responders():
    broker = InMemoryBroker()
    nc = await broker.connect()
    client = _client_class(Logs)(nc=nc, verify=False)

    with pytest.raises(RpcNoRespondersError):
        await asyncio.wait_for(_collect(client.tail(n=1)), WITHIN)
    await nc.close()


@contextlib.asynccontextmanager
async def faking(
    *answers: tuple[bytes, dict[str, str]], pause: float = 0.0, timeout: float = 30.0
) -> AsyncIterator[ServiceClient]:
    """A fake `logs` that answers `tail` with exactly `answers`, `pause` apart, and reads no
    header, its budget included; and a client of it."""
    broker = InMemoryBroker()
    service_nc = await broker.connect()

    async def answer(msg: Any) -> None:
        for data, headers in answers:
            await service_nc.publish(msg.reply, data, headers=headers)
            await asyncio.sleep(pause)

    await service_nc.subscribe("logs.rpc.tail", cb=answer)
    nc = await broker.connect()
    try:
        yield _client_class(Logs)(nc=nc, verify=False, subject_prefix="", timeout=timeout)
    finally:
        await nc.close()
        await service_nc.close()


def _chunk(seq: int, item: Any) -> tuple[bytes, dict[str, str]]:
    return json.dumps(item).encode(), {"Content-Type": "application/json", SEQ: str(seq)}


def _end(count: int) -> tuple[bytes, dict[str, str]]:
    body = {"success": True, "result": None, "items": count}
    return json.dumps(body).encode(), {"Content-Type": "application/json", END: str(count)}


LINE = {"number": 0, "text": "a"}


async def test_the_whole_stream_is_bounded_by_the_clients_own_timeout():
    """The fake ignores the budget it is sent, so only the client's own bound can end this."""
    chunks = [_chunk(seq, LINE) for seq in range(1000)]
    got: list[Any] = []

    async def read(client: Any) -> None:
        async for line in client.tail(n=1000):
            got.append(line)

    async with faking(*chunks, pause=0.02, timeout=0.2) as client:
        with pytest.raises(RpcTimeoutError) as raised:
            await asyncio.wait_for(read(client), WITHIN)

    assert type(raised.value) is RpcTimeoutError
    assert 0 < len(got) < 1000
    assert raised.value.items == len(got)


async def test_a_stream_that_falls_silent_ends_at_the_clients_timeout():
    """One item, then nothing: the wait for the next message is itself bounded."""
    got: list[Any] = []

    async def read(client: Any) -> None:
        async for line in client.tail(n=2):
            got.append(line)

    async with faking(_chunk(0, LINE), timeout=0.2) as client:
        began = time.monotonic()
        with pytest.raises(RpcTimeoutError) as raised:
            await asyncio.wait_for(read(client), WITHIN)
        took = time.monotonic() - began

    assert type(raised.value) is RpcTimeoutError
    assert (len(got), raised.value.items) == (1, 1)
    assert took < 1.0, f"the silent stream ended after {took:.3f}s, not at its 0.2s timeout"


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param((_chunk(0, LINE), _chunk(2, LINE)), id="a-gap"),
        pytest.param((_chunk(0, LINE), _chunk(1, "not a line")), id="an-item-of-the-wrong-type"),
    ],
)
async def test_a_stream_that_fails_mid_way_unsubscribes_its_inbox(answers):
    async with faking(*answers) as client:
        nc = client._nc
        before = set(nc.subscriptions)
        with pytest.raises((RpcStreamGapError, RpcServerError)):
            await asyncio.wait_for(_collect(client.tail(n=2)), WITHIN)
        left = set(nc.subscriptions) - before

    assert left == set(), f"the inbox is still subscribed after the stream failed: {left}"


async def test_an_end_that_does_not_say_success_is_the_services_fault():
    unsaid = (json.dumps({"result": None, "items": 1}).encode(), {END: "1"})
    async with faking(_chunk(0, LINE), unsaid) as client:
        with pytest.raises(RpcServerError, match="without success") as raised:
            await asyncio.wait_for(_collect(client.tail(n=1)), WITHIN)

    assert raised.value.items == 1


@pytest.mark.parametrize(
    ("body", "kind"), [(b'["done"]', "list"), (b'"done"', "str")], ids=["a-list", "a-string"]
)
async def test_an_end_that_is_not_an_object_is_the_services_fault_by_name(body, kind):
    async with faking(_chunk(0, LINE), (body, {END: "1"})) as client:
        with pytest.raises(
            RpcServerError, match=rf"ended its stream with {kind}, not an object: "
        ) as raised:
            await asyncio.wait_for(_collect(client.tail(n=1)), WITHIN)

    assert raised.value.items == 1


async def test_an_item_that_never_arrived_is_a_gap():
    async with faking(_chunk(0, LINE), _chunk(1, LINE), _chunk(3, LINE), _end(4)) as client:
        got: list[Any] = []
        with pytest.raises(RpcStreamGapError) as raised:
            async for line in client.tail(n=4):
                got.append(line)

    assert (raised.value.expected, raised.value.got, raised.value.items) == (2, 3, 2)
    assert len(got) == 2


async def test_an_end_that_counts_more_items_than_arrived_is_a_gap():
    async with faking(_chunk(0, LINE), _chunk(1, LINE), _end(3)) as client:
        with pytest.raises(RpcStreamGapError) as raised:
            await asyncio.wait_for(_collect(client.tail(n=3)), WITHIN)

    assert (raised.value.expected, raised.value.got, raised.value.items) == (3, 2, 2)


async def test_an_item_that_does_not_match_its_type_is_the_services_fault():
    async with faking(_chunk(0, LINE), _chunk(1, "not a line"), _end(2)) as client:
        with pytest.raises(RpcServerError, match="item 1") as raised:
            await asyncio.wait_for(_collect(client.tail(n=2)), WITHIN)

    assert raised.value.items == 1


async def test_a_message_that_is_neither_an_item_nor_the_end_is_the_services_fault():
    stray = (b"{}", {"Content-Type": "application/json"})
    async with faking(_chunk(0, LINE), stray) as client:
        with pytest.raises(RpcServerError, match="neither an item nor the end") as raised:
            await asyncio.wait_for(_collect(client.tail(n=2)), WITHIN)

    assert raised.value.items == 1


async def test_an_out_of_date_client_refused_before_any_item_learns_it_is_out_of_date():
    class OldLogs(CliffracerService):
        @rpc
        async def tail(self, n: str) -> AsyncIterator[Line]:
            yield Line(number=0, text=n)

    broker = InMemoryBroker()
    async with ServiceTestHarness(Logs(), broker=broker):
        nc = await broker.connect()
        client = _client_class(OldLogs)(nc=nc, service="logs", subject_prefix="")
        client._verified = True  # verified before the service moved
        with pytest.raises(ClientOutOfDateError):
            await asyncio.wait_for(_collect(client.tail(n="three")), WITHIN)
        await nc.close()


async def test_a_handler_streaming_from_another_service_spends_its_own_budget():
    reader = Reader()
    broker = InMemoryBroker()
    async with ServiceTestHarness(Logs(), broker=broker), ServiceTestHarness(reader, broker=broker):
        nc = await broker.connect()
        reply = await nc.request(
            "reader.rpc.numbers",
            json.dumps({"n": 3}).encode(),
            timeout=WITHIN,
            headers={"Content-Type": "application/json", "Cliffracer-Timeout-Ms": "2000"},
        )
        await nc.close()

    assert json.loads(reply.data)["result"] == [0, 1, 2]
    sent = next(p for p in broker.published if p.subject == "logs.rpc.tail")
    assert sent.headers is not None
    assert 0 < int(sent.headers["Cliffracer-Timeout-Ms"]) <= 2000


async def test_a_send_hook_sees_one_call_for_a_stream_of_many_items():
    calls, before, after = recorder()

    class Hooked(Reader):
        recording = calls

    reader = Hooked()
    broker = InMemoryBroker()
    async with ServiceTestHarness(Logs(), broker=broker), ServiceTestHarness(reader, broker=broker):
        got = await asyncio.wait_for(_collect(reader.stream_rpc("logs", "tail", n=5)), WITHIN)

    assert len(got) == 5
    assert (before, after) == (["stream_rpc"], [("stream_rpc", None)])


async def test_a_proxy_streams_and_a_plain_call_of_a_streaming_method_names_stream():
    reader = Reader()
    broker = InMemoryBroker()
    async with ServiceTestHarness(Logs(), broker=broker), ServiceTestHarness(reader, broker=broker):
        lines = await asyncio.wait_for(_collect(reader.logs.tail.stream(n=2)), WITHIN)
        with pytest.raises(RpcValidationError, match=r"`\.tail\.stream\(\.\.\.\)`"):
            await reader.logs.tail(n=2)

    assert [line["number"] for line in lines] == [0, 1]


def _inboxes(nc: Any) -> set[str]:
    return {sub.subject for sub in nc.subscriptions if sub.subject.startswith("_INBOX.")}


async def test_aclosing_a_generated_clients_stream_unsubscribes_its_inbox_at_once():
    async with serving() as (_, _, client):
        nc = client._nc
        before = _inboxes(nc)
        async with contextlib.aclosing(client.tail(n=1000, pause=0.005)) as lines:
            async for _ in lines:
                break
        left = _inboxes(nc) - before

    assert left == set(), f"still subscribed when the aclosing block exited: {left}"


@pytest.mark.parametrize("way", ["stream_rpc", "proxy"])
async def test_aclosing_a_services_stream_unsubscribes_its_inbox_at_once(way):
    reader = Reader()
    broker = InMemoryBroker()
    async with ServiceTestHarness(Logs(), broker=broker), ServiceTestHarness(reader, broker=broker):
        nc = reader.nc
        before = _inboxes(nc)
        stream = (
            reader.stream_rpc("logs", "tail", n=1000, pause=0.005)
            if way == "stream_rpc"
            else reader.logs.tail.stream(n=1000, pause=0.005)
        )
        async with contextlib.aclosing(stream) as lines:
            async for _ in lines:
                break
        left = _inboxes(nc) - before

    assert left == set(), f"still subscribed when the aclosing block exited: {left}"


async def test_a_generated_clients_stream_inside_a_handler_sends_what_its_request_has_left():
    async with serving(timeout=30.0) as (_, broker, client):
        loop = asyncio.get_running_loop()
        with deadlines.scoped(Deadline(loop.time() + 2.0, 2.0, "caller")):
            await asyncio.wait_for(_collect(client.tail(n=1)), WITHIN)
        sent = next(p for p in broker.published if p.subject == "logs.rpc.tail")

    assert sent.headers is not None
    assert 0 < int(sent.headers["Cliffracer-Timeout-Ms"]) <= 2000

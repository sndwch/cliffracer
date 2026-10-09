"""`cliffracer.calls.call` and `stream`: a service called without a generated client.

They send what `call_rpc` and `stream_rpc` send (the subject with its namespace and prefix,
`Content-Type: application/json`, the correlation id, the budget cut to what an enclosing request
has left) and raise what they raise. They refuse, before sending, a timeout that is not a positive
finite number, an argument JSON cannot carry, and a model, whose wire form is the generated
client's to choose.

A real service runs on the in-memory broker. A fake one, a plain subscriber answering on the
method's subject, sends what a real one would not: silence, a gap, a pause.
"""

import asyncio
import contextlib
import json
import math
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel

from cliffracer import (
    CliffracerService,
    RpcClientError,
    RpcDeadlineExceededError,
    RpcNoRespondersError,
    RpcRefusedError,
    RpcServerError,
    RpcStreamGapError,
    RpcTimeoutError,
    RpcUnknownMethodError,
    RpcValidationError,
    ServiceConfig,
    rpc,
)
from cliffracer.calls import call, prepare, stream
from cliffracer.core import deadline as deadlines
from cliffracer.core.correlation import correlation_id_var
from cliffracer.core.deadline import Deadline
from cliffracer.core.extension import RejectMessage
from cliffracer.testing import InMemoryBroker, ServiceTestHarness

pytestmark = pytest.mark.unit

#: Bound on any wait here, so a call that never ends fails by name.
WITHIN = 5.0
SEQ, END = "Cliffracer-Stream-Seq", "Cliffracer-Stream-End"


class Item(BaseModel):
    n: int


class Sums(CliffracerService):
    def __init__(self, **config: Any) -> None:
        super().__init__(ServiceConfig(name="sums", subject_prefix=None, health_port=0, **config))

    @rpc
    async def add(self, a: int, b: int) -> int:
        return a + b

    @rpc
    async def refuse(self) -> int:
        raise RejectMessage("not for you")

    @rpc
    async def crash(self) -> int:
        raise RuntimeError("broken")

    @rpc
    async def slow(self) -> int:
        await asyncio.sleep(10)
        return 0

    @rpc
    async def count(self, n: int) -> AsyncIterator[int]:
        for value in range(n):
            yield value

    @rpc
    async def count_then_crash(self) -> AsyncIterator[int]:
        yield 0
        yield 1
        raise RuntimeError("broken")


@contextlib.asynccontextmanager
async def serving(**config: Any) -> AsyncIterator[tuple[InMemoryBroker, Any]]:
    broker = InMemoryBroker()
    async with ServiceTestHarness(Sums(**config), broker=broker):
        nc = await broker.connect()
        try:
            yield broker, nc
        finally:
            await nc.close()


def sent_to(broker: InMemoryBroker, subject: str) -> list[Any]:
    return [p for p in broker.published if p.subject == subject]


async def _collect(items: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in items]


# --- call ----------------------------------------------------------------------------------------


async def test_a_call_returns_the_result_and_sends_json_its_correlation_id_and_its_budget():
    async with serving() as (broker, nc):
        result = await call(nc, "sums", "add", {"a": 2, "b": 3}, subject_prefix="", timeout=7.0)

    assert result == 5
    (request,) = sent_to(broker, "sums.rpc.add")
    assert json.loads(request.data) == {"a": 2, "b": 3}
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Cliffracer-Timeout-Ms"] == "7000"
    assert request.headers["X-Correlation-ID"] == request.headers["correlation_id"]


async def test_a_header_the_caller_gives_is_sent_and_its_correlation_id_is_used():
    async with serving() as (broker, nc):
        await call(
            nc,
            "sums",
            "add",
            {"a": 1, "b": 1},
            subject_prefix="",
            headers={"x-correlation-id": "mine", "Authorization": "Bearer t"},
        )

    (request,) = sent_to(broker, "sums.rpc.add")
    assert request.headers["X-Correlation-ID"] == "mine"
    assert request.headers["Authorization"] == "Bearer t"


async def test_a_call_made_where_a_correlation_id_is_ambient_carries_it():
    token = correlation_id_var.set("ambient-trace")
    try:
        async with serving() as (broker, nc):
            await call(nc, "sums", "add", {"a": 1, "b": 1}, subject_prefix="")
    finally:
        correlation_id_var.reset(token)

    (request,) = sent_to(broker, "sums.rpc.add")
    assert request.headers["X-Correlation-ID"] == "ambient-trace"


@pytest.mark.parametrize("name", ["Cliffracer-Timeout-Ms", "cliffracer-timeout-ms"])
async def test_a_budget_the_caller_gives_is_sent_as_given_and_once(name):
    async with serving() as (broker, nc):
        await call(nc, "sums", "add", {"a": 1, "b": 1}, subject_prefix="", headers={name: "777"})

    (request,) = sent_to(broker, "sums.rpc.add")
    budgets = {k: v for k, v in request.headers.items() if k.lower() == "cliffracer-timeout-ms"}
    assert budgets == {name: "777"}


@pytest.mark.parametrize(
    ("method", "params", "error"),
    [
        pytest.param("add", {"a": "x", "b": 1}, RpcValidationError, id="validation_failed"),
        pytest.param("nope", {}, RpcUnknownMethodError, id="unknown_method"),
        pytest.param("refuse", {}, RpcRefusedError, id="refused"),
        pytest.param("crash", {}, RpcServerError, id="internal"),
    ],
)
async def test_an_error_the_service_answers_is_raised_as_call_rpc_raises_it(method, params, error):
    async with serving() as (_, nc):
        with pytest.raises(error):
            await call(nc, "sums", method, params, subject_prefix="")


async def test_a_handler_the_service_cuts_off_is_raised_as_deadline_exceeded():
    async with serving(max_rpc_processing_time=0.1) as (_, nc):
        with pytest.raises(RpcDeadlineExceededError):
            await call(nc, "sums", "slow", subject_prefix="")


async def test_a_call_nothing_holds_raises_no_responders():
    broker = InMemoryBroker()
    nc = await broker.connect()
    with pytest.raises(RpcNoRespondersError):
        await call(nc, "sums", "add", {"a": 1, "b": 1}, subject_prefix="")
    await nc.close()


async def test_a_call_nobody_answers_raises_a_timeout_at_its_own_bound():
    broker = InMemoryBroker()
    silent, nc = await broker.connect(), await broker.connect()

    async def ignore(_msg: Any) -> None:
        return None

    await silent.subscribe("sums.rpc.add", cb=ignore)
    with pytest.raises(RpcTimeoutError, match="did not answer within 0.2s"):
        await asyncio.wait_for(
            call(nc, "sums", "add", {"a": 1, "b": 1}, subject_prefix="", timeout=0.2), WITHIN
        )
    await nc.close()
    await silent.close()


async def test_a_call_inside_a_request_sends_what_is_left_and_nothing_once_it_is_spent():
    loop = asyncio.get_running_loop()
    async with serving() as (broker, nc):
        with deadlines.scoped(Deadline(loop.time() + 2.0, 2.0, "caller")):
            await call(nc, "sums", "add", {"a": 1, "b": 1}, subject_prefix="")
        with deadlines.scoped(Deadline(loop.time() - 1.0, 1.0, "caller")):
            with pytest.raises(RpcTimeoutError, match="was not sent"):
                await call(nc, "sums", "add", {"a": 1, "b": 1}, subject_prefix="")

    (request,) = sent_to(broker, "sums.rpc.add")
    assert 0 < int(request.headers["Cliffracer-Timeout-Ms"]) <= 2000


async def test_the_namespace_and_the_environment_prefix_address_the_subject(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "east")
    broker = InMemoryBroker()
    nc = await broker.connect()
    with pytest.raises(RpcNoRespondersError, match="east.retail.sums.rpc.add"):
        await call(nc, "sums", "add", {"a": 1, "b": 1}, namespace="retail")
    await nc.close()


async def test_a_service_reading_another_format_takes_the_json_call():
    """The request is labelled JSON, so a service configured for msgpack reads it by that label."""
    async with serving(serialization_format="msgpack") as (_, nc):
        assert await call(nc, "sums", "add", {"a": 2, "b": 3}, subject_prefix="") == 5


# --- refused before sending -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"item": Item(n=1)}, id="a-model"),
        pytest.param({"items": [{"inner": Item(n=1)}]}, id="a-model-in-a-list"),
    ],
)
async def test_a_model_in_the_arguments_is_refused_pointing_at_the_generated_client(params):
    async with serving() as (broker, nc):
        with pytest.raises(RpcClientError, match="generated client") as refused:
            await call(nc, "sums", "add", params, subject_prefix="")
        with pytest.raises(RpcClientError, match="generated client"):
            stream(nc, "sums", "count", params, subject_prefix="")

    assert "Item" in str(refused.value)
    assert sent_to(broker, "sums.rpc.add") == []


async def test_an_argument_json_cannot_carry_is_refused_before_sending():
    async with serving() as (broker, nc):
        with pytest.raises(RpcClientError, match="cannot be encoded"):
            await call(nc, "sums", "add", {"a": object()}, subject_prefix="")

    assert sent_to(broker, "sums.rpc.add") == []


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf, True, "5"])
async def test_a_timeout_that_is_not_a_positive_finite_number_is_refused_by_name(bad):
    async with serving() as (broker, nc):
        with pytest.raises(ValueError, match="^timeout must be a positive, finite number"):
            await call(nc, "sums", "add", {"a": 1, "b": 1}, subject_prefix="", timeout=bad)
        with pytest.raises(ValueError, match="^timeout must be a positive, finite number"):
            stream(nc, "sums", "count", {"n": 1}, subject_prefix="", timeout=bad)
        with pytest.raises(ValueError, match="^idle_timeout must be a positive, finite number"):
            stream(nc, "sums", "count", {"n": 1}, subject_prefix="", idle_timeout=bad)

    assert broker.published == ()


@pytest.mark.parametrize(
    ("service", "method", "said"),
    [
        pytest.param("sums", "a b", "whitespace", id="a-space-in-the-method"),
        pytest.param("su ms", "add", "whitespace", id="a-space-in-the-service"),
        pytest.param("sums", "", "empty token", id="an-empty-method"),
        pytest.param("sums", "a..b", "empty token", id="a-doubled-dot"),
    ],
)
async def test_a_subject_the_server_cannot_use_is_refused_before_sending(service, method, said):
    """A subject with whitespace makes the server close the caller's whole connection, and one
    with an empty token is answered by nobody: both are refused here, as `call_rpc` refuses them."""
    async with serving() as (broker, nc):
        with pytest.raises(ValueError, match=f"Invalid RPC subject .*{said}"):
            await call(nc, service, method, subject_prefix="")
        with pytest.raises(ValueError, match=f"Invalid RPC subject .*{said}"):
            stream(nc, service, method, subject_prefix="")

    assert broker.published == ()


# --- stream --------------------------------------------------------------------------------------


async def test_a_stream_yields_each_item_and_sends_the_stream_header():
    async with serving() as (broker, nc):
        items = await asyncio.wait_for(
            _collect(stream(nc, "sums", "count", {"n": 3}, subject_prefix="")), WITHIN
        )

    assert items == [0, 1, 2]
    (request,) = sent_to(broker, "sums.rpc.count")
    assert (request.headers["Cliffracer-Stream"], request.headers["Content-Type"]) == (
        "1",
        "application/json",
    )


async def test_an_error_a_stream_ends_with_is_raised_after_its_items_with_their_count():
    got: list[Any] = []
    async with serving() as (_, nc):
        with pytest.raises(RpcServerError) as raised:
            async for item in stream(nc, "sums", "count_then_crash", subject_prefix=""):
                got.append(item)

    assert (got, raised.value.items) == ([0, 1], 2)


@contextlib.asynccontextmanager
async def faking(
    *answers: tuple[bytes, dict[str, str]], pause_after: int = -1, pause: float = 0.0
) -> AsyncIterator[Any]:
    """A fake `sums.count` that sends `answers`, pausing `pause` after the one at `pause_after`."""
    broker = InMemoryBroker()
    service_nc, nc = await broker.connect(), await broker.connect()

    async def answer(msg: Any) -> None:
        for index, (data, headers) in enumerate(answers):
            await service_nc.publish(msg.reply, data, headers=headers)
            if index == pause_after:
                await asyncio.sleep(pause)

    await service_nc.subscribe("sums.rpc.count", cb=answer)
    try:
        yield nc
    finally:
        await nc.close()
        await service_nc.close()


def _chunk(seq: int) -> tuple[bytes, dict[str, str]]:
    return json.dumps(seq).encode(), {"Content-Type": "application/json", SEQ: str(seq)}


def _end(count: int) -> tuple[bytes, dict[str, str]]:
    body = {"success": True, "result": None, "items": count}
    return json.dumps(body).encode(), {"Content-Type": "application/json", END: str(count)}


async def test_a_stream_that_falls_silent_for_its_idle_timeout_ends_naming_it():
    """The whole timeout is far off: only the idle bound can end this."""
    got: list[Any] = []

    async def read(nc: Any) -> None:
        async for item in stream(
            nc, "sums", "count", subject_prefix="", timeout=30.0, idle_timeout=0.1
        ):
            got.append(item)

    async with faking(_chunk(0), _chunk(1), _end(2), pause_after=0, pause=2.0) as nc:
        with pytest.raises(RpcTimeoutError, match="sent nothing for 0.1s") as raised:
            await asyncio.wait_for(read(nc), WITHIN)

    assert (got, raised.value.items) == ([0], 1)


async def test_a_stream_that_sends_nothing_at_all_ends_by_its_idle_timeout():
    """No item ever arrives: the idle bound applies to the wait for the first one too."""
    async with faking() as nc:
        with pytest.raises(RpcTimeoutError, match="sent nothing for 0.1s") as raised:
            await asyncio.wait_for(
                _collect(
                    stream(nc, "sums", "count", subject_prefix="", timeout=30.0, idle_timeout=0.1)
                ),
                WITHIN,
            )

    assert raised.value.items == 0


async def test_an_idle_timeout_longer_than_each_gap_lets_the_stream_end():
    async with faking(_chunk(0), _chunk(1), _end(2), pause_after=0, pause=0.05) as nc:
        items = await asyncio.wait_for(
            _collect(stream(nc, "sums", "count", subject_prefix="", timeout=5.0, idle_timeout=1.0)),
            WITHIN,
        )

    assert items == [0, 1]


async def test_with_no_idle_timeout_a_silent_stream_ends_at_its_whole_timeout():
    async with faking(_chunk(0), _chunk(1), _end(2), pause_after=0, pause=2.0) as nc:
        with pytest.raises(RpcTimeoutError, match="did not end within 0.2s"):
            await asyncio.wait_for(
                _collect(stream(nc, "sums", "count", subject_prefix="", timeout=0.2)), WITHIN
            )


async def test_an_item_that_never_arrived_is_a_gap():
    async with faking(_chunk(0), _chunk(2), _end(3)) as nc:
        with pytest.raises(RpcStreamGapError):
            await asyncio.wait_for(_collect(stream(nc, "sums", "count", subject_prefix="")), WITHIN)


async def test_leaving_a_stream_under_aclosing_unsubscribes_its_inbox_at_once():
    async with serving() as (_, nc):
        before = {s.subject for s in nc.subscriptions}
        async with contextlib.aclosing(
            stream(nc, "sums", "count", {"n": 1000}, subject_prefix="")
        ) as items:
            async for _ in items:
                break
        left = {s.subject for s in nc.subscriptions} - before

    assert left == set(), left


# --- prepare and timeout=None --------------------------------------------------------------------


async def test_what_prepare_builds_is_what_call_sends():
    async with serving() as (broker, nc):
        prepared = prepare(
            "sums", "add", {"a": 2, "b": 3}, subject_prefix="", headers={"X-Correlation-ID": "c1"}
        )
        await call(
            nc,
            "sums",
            "add",
            {"a": 2, "b": 3},
            subject_prefix="",
            headers={"X-Correlation-ID": "c1"},
        )

    (request,) = sent_to(broker, "sums.rpc.add")
    assert (request.subject, dict(request.headers), request.data) == (
        prepared.subject,
        prepared.headers,
        prepared.payload,
    )
    assert prepared.timeout == 30.0


async def test_no_timeout_sends_no_budget_and_waits_with_no_bound():
    async with serving() as (broker, nc):
        prepared = prepare("sums", "add", {"a": 1, "b": 1}, subject_prefix="", timeout=None)
        result = await call(nc, "sums", "add", {"a": 1, "b": 1}, subject_prefix="", timeout=None)
        items = await asyncio.wait_for(
            _collect(stream(nc, "sums", "count", {"n": 2}, subject_prefix="", timeout=None)), WITHIN
        )

    assert (result, items, prepared.timeout) == (2, [0, 1], None)
    assert all(
        "Cliffracer-Timeout-Ms" not in (p.headers or {})
        for p in broker.published
        if ".rpc." in p.subject
    ), [dict(p.headers or {}) for p in broker.published]


async def test_no_timeout_inside_a_request_waits_and_sends_what_the_request_has_left():
    loop = asyncio.get_running_loop()
    with deadlines.scoped(Deadline(loop.time() + 2.0, 2.0, "caller")):
        prepared = prepare("sums", "add", {"a": 1, "b": 1}, subject_prefix="", timeout=None)

    assert prepared.timeout is not None and 0 < prepared.timeout <= 2.0
    assert 0 < int(prepared.headers["Cliffracer-Timeout-Ms"]) <= 2000

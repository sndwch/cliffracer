"""A handler declared with `max_concurrency=n` runs at most `n` calls at once, on every path.

The limit is declared on the decorator (`@rpc(max_concurrency=n)`, `@listener(...,
max_concurrency=n)`), one per method across all its subjects. A request takes its handler's permit
first and the service's second, so one that waits at a full handler holds nothing another method
needs; both waits are bounded by the request's deadline. What is happening at each limit is in
`/health`'s `handler_limits`.
"""

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.aio.msg import Msg
from pydantic import BaseModel

from cliffracer import (
    CliffracerService,
    ConfigurationError,
    ServiceConfig,
    async_rpc,
    broadcast,
    listener,
    rpc,
    validated_listener,
)
from cliffracer.core.deadline import TIMEOUT_HEADER
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

#: The outer bound on a wait that only a defect can make long.
NEVER = 10.0


class Desk(CliffracerService):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.release = asyncio.Event()
        self.running = 0
        self.most_running = 0
        self.ran: list[str] = []

    async def _hold(self, name: str) -> int:
        self.ran.append(name)
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            await self.release.wait()
        finally:
            self.running -= 1
        return 1

    @rpc(max_concurrency=2)
    async def report(self) -> int:
        return await self._hold("report")

    @rpc(max_concurrency=1)
    async def single(self) -> int:
        return await self._hold("single")

    @rpc
    async def unlimited(self) -> int:
        return await self._hold("unlimited")

    @rpc
    async def quick(self) -> int:
        self.ran.append("quick")
        return 2


def _desk(**config: Any) -> Desk:
    service = Desk(ServiceConfig(name="desk", health_port=0, **config))
    service._discover_handlers()
    service.container.lifecycle._running = True
    return service


def _msg(method: str, kind: str = "rpc", **headers: Any) -> MockMessage:
    return MockMessage(subject=f"desk.{kind}.{method}", data=b"{}", headers=headers)


def _reply(msg: MockMessage) -> dict | None:
    return None if msg.responded_data is None else json.loads(msg.responded_data)


async def _until(condition) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), NEVER)


async def _drain(service: CliffracerService) -> None:
    tasks = list(service.container._active_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), NEVER)


def _limits(service: CliffracerService) -> dict[str, dict[str, int]]:
    return service.container.dispatcher.limits.details()


# --- the declaration ------------------------------------------------------------------------


class Payload(BaseModel):
    n: int = 0


def test_each_decorator_records_its_methods_limit_and_bare_rpc_records_none():
    async def handler(self, subject: str) -> None: ...

    def fresh():
        async def f(self) -> None: ...

        return f

    assert not hasattr(rpc(fresh()), "_cliffracer_max_concurrency")
    assert rpc(fresh())._cliffracer_rpc is True
    assert rpc(max_concurrency=2)(fresh())._cliffracer_max_concurrency == 2
    marked = async_rpc(max_concurrency=3)(fresh())
    assert (marked._cliffracer_max_concurrency, marked._cliffracer_async_rpc) == (3, True)

    async def a(self, subject: str) -> None: ...

    async def b(self, message: Payload) -> None: ...

    async def c(self, subject: str) -> None: ...

    assert listener("x.y", fanout=True, max_concurrency=4)(a)._cliffracer_max_concurrency == 4
    assert (
        validated_listener("x.y", Payload, fanout=True, max_concurrency=5)(
            b
        )._cliffracer_max_concurrency
        == 5
    )
    assert broadcast("x.y", max_concurrency=6)(c)._cliffracer_max_concurrency == 6


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "2"])
def test_a_limit_that_is_not_a_positive_int_is_refused_when_declared(bad):
    async def f(self) -> None: ...

    with pytest.raises(ConfigurationError, match="max_concurrency=.*a positive int"):
        rpc(max_concurrency=bad)(f)
    with pytest.raises(ConfigurationError, match="max_concurrency=.*a positive int"):
        listener("x.y", fanout=True, max_concurrency=bad)(f)


def test_a_limit_passed_where_the_handler_goes_is_refused_naming_the_keyword():
    with pytest.raises(ConfigurationError, match=r"@rpc\(max_concurrency=4\)"):
        rpc(4)


def test_one_method_has_one_limit_across_its_decorators():
    async def f(self, subject: str) -> None: ...

    same = listener("x.y", fanout=True, max_concurrency=2)(rpc(max_concurrency=2)(f))
    assert same._cliffracer_max_concurrency == 2

    async def g(self, subject: str) -> None: ...

    with pytest.raises(ConfigurationError, match="another decorator on the same method gave 2"):
        listener("x.y", fanout=True, max_concurrency=3)(rpc(max_concurrency=2)(g))


# --- request-reply and fire-and-forget RPC ----------------------------------------------------


async def test_a_limited_method_runs_at_most_its_limit_and_the_rest_wait_then_finish():
    service = _desk()
    msgs = [_msg("report") for _ in range(5)]
    for msg in msgs:
        await asyncio.wait_for(service.container.dispatcher.on_rpc_request(msg), 1.0)
    await _until(lambda: _limits(service)["report"]["waiting"] == 3)

    assert (service.running, _limits(service)["report"]) == (
        2,
        {"limit": 2, "max_queued": None, "in_flight": 2, "waiting": 3, "refused": 0},
    )
    service.release.set()
    await _drain(service)

    assert [(_reply(m) or {}).get("result") for m in msgs] == [1] * 5
    assert service.most_running == 2
    assert _limits(service)["report"] == {
        "limit": 2,
        "max_queued": None,
        "in_flight": 0,
        "waiting": 0,
        "refused": 0,
    }


async def test_CONTROL_a_method_with_no_limit_runs_every_request_at_once():
    service = _desk()
    for _ in range(5):
        await service.container.dispatcher.on_rpc_request(_msg("unlimited"))
    await _until(lambda: service.running == 5)
    service.release.set()
    await _drain(service)

    assert service.most_running == 5
    assert "unlimited" not in _limits(service)


async def test_requests_waiting_at_a_full_method_hold_no_service_permit_another_method_needs():
    # Two service permits. `single` holds one; the requests queued behind it wait for `single`'s
    # own permit, holding nothing, so `quick` gets the second service permit and runs. Were the
    # service's permit taken first, each waiting request would hold one, and `quick` would wait.
    service = _desk(max_rpc_concurrency=2)
    on_request = service.container.dispatcher.on_rpc_request
    held = [_msg("single") for _ in range(3)]
    for msg in held:
        await on_request(msg)
    await _until(lambda: service.ran == ["single"] and _limits(service)["single"]["waiting"] == 2)

    other = _msg("quick")
    await on_request(other)
    await _until(lambda: other.responded_data is not None)

    assert (_reply(other) or {}).get("result") == 2
    assert service.ran == ["single", "quick"]
    service.release.set()
    await _drain(service)
    assert [(_reply(m) or {}).get("result") for m in held] == [1, 1, 1]
    assert service.most_running == 1


async def test_a_request_whose_deadline_passes_at_a_full_method_is_answered_and_counted():
    service = _desk()
    on_request = service.container.dispatcher.on_rpc_request
    holder = _msg("single")
    await on_request(holder)
    await _until(lambda: service.ran == ["single"])

    late = _msg("single", **{TIMEOUT_HEADER: "50"})
    await on_request(late)
    await _until(lambda: late.responded_data is not None)

    reply = _reply(late) or {}
    assert (reply.get("code"), reply.get("set_by")) == ("deadline_exceeded", "caller")
    assert "was not started" in reply.get("error", "")
    assert service.ran == ["single"]
    assert _limits(service)["single"] == {
        "limit": 1,
        "max_queued": None,
        "in_flight": 1,
        "waiting": 0,
        "refused": 1,
    }
    service.release.set()
    await _drain(service)


async def test_a_handler_permit_is_returned_when_the_service_permit_never_comes():
    # One service permit, held by `unlimited`. A `report` request takes one of its two handler
    # permits and waits for the service's until its deadline passes; it must give that one back.
    service = _desk(max_rpc_concurrency=1)
    on_request = service.container.dispatcher.on_rpc_request
    await on_request(_msg("unlimited"))
    await _until(lambda: service.ran == ["unlimited"])

    late = _msg("report", **{TIMEOUT_HEADER: "50"})
    await on_request(late)
    await _until(lambda: late.responded_data is not None)

    assert (_reply(late) or {}).get("code") == "deadline_exceeded"
    report = service.container.dispatcher.limits.of(service.report)
    assert report is not None and report.sem._value == 2, "a handler permit was kept"
    assert report.details() == {
        "limit": 2,
        "max_queued": None,
        "in_flight": 0,
        "waiting": 0,
        "refused": 1,
    }
    service.release.set()
    await _drain(service)


async def test_the_async_subject_shares_its_methods_limit():
    service = _desk()
    dispatcher = service.container.dispatcher
    await dispatcher.on_rpc_request(_msg("single"))
    await dispatcher.on_async_request(_msg("single", kind="async"))
    await dispatcher.on_async_request(_msg("single", kind="async"))
    await _until(lambda: _limits(service)["single"]["waiting"] == 2)

    assert service.running == 1
    service.release.set()
    await _drain(service)
    assert service.ran == ["single"] * 3
    assert service.most_running == 1


async def test_a_request_holding_its_permits_while_the_service_stops_is_counted_refused():
    service = _desk()
    on_request = service.container.dispatcher.on_rpc_request
    await on_request(_msg("single"))
    await _until(lambda: service.ran == ["single"])
    queued = _msg("single")
    await on_request(queued)
    await _until(lambda: _limits(service)["single"]["waiting"] == 1)

    service.container.lifecycle._stop_requests += 1
    service.release.set()
    await _drain(service)

    assert (_reply(queued) or {}).get("code") == "busy"
    assert service.ran == ["single"]
    assert _limits(service)["single"]["refused"] == 1


# --- /health ----------------------------------------------------------------------------------


async def test_health_names_every_declared_limit_before_any_request():
    service = Desk(ServiceConfig(name="desk", health_port=0))
    service._discover_handlers()

    health = await service.health_check()

    assert health["handler_limits"] == {
        "report": {"limit": 2, "max_queued": None, "in_flight": 0, "waiting": 0, "refused": 0},
        "single": {"limit": 1, "max_queued": None, "in_flight": 0, "waiting": 0, "refused": 0},
    }


async def test_CONTROL_a_service_with_no_limit_has_no_handler_limits_in_its_health():
    class Plain(CliffracerService):
        @rpc
        async def ping(self) -> int:
            return 1

    service = Plain(ServiceConfig(name="plain", health_port=0))
    service._discover_handlers()

    assert "handler_limits" not in await service.health_check()


# --- events -----------------------------------------------------------------------------------

STATE: dict = {}


def _note_running() -> None:
    STATE["running"] += 1
    STATE["most_running"] = max(STATE["most_running"], STATE["running"])


class Listening(CliffracerService):
    @listener("events.order", fanout=True, max_concurrency=1)
    async def on_order(self, subject: str, number: int = 0) -> None:
        _note_running()
        STATE["handled"].append(number)
        try:
            await STATE["release"].wait()
        finally:
            STATE["running"] -= 1


def _reset_state() -> None:
    STATE.clear()
    STATE.update(running=0, most_running=0, handled=[], release=asyncio.Event())


async def test_a_core_event_listener_runs_at_most_its_limit_and_the_rest_wait_then_run():
    _reset_state()
    service = Listening(ServiceConfig(name="orders", health_port=0))
    service._discover_handlers()
    callback = service.container.dispatcher.make_event_callback("events.order")

    async def subscription() -> None:
        # nats-py hands this subscription's messages to its callback one at a time.
        for number in range(3):
            await callback(
                MockMessage(
                    subject="events.order", data=json.dumps({"number": number}).encode(), headers={}
                )
            )

    delivering = asyncio.create_task(subscription())
    await _until(lambda: STATE["handled"] == [0] and _limits(service)["on_order"]["waiting"] == 1)
    assert STATE["running"] == 1
    STATE["release"].set()
    await asyncio.wait_for(delivering, NEVER)
    await _drain(service)

    assert STATE["handled"] == [0, 1, 2]
    assert STATE["most_running"] == 1


class Durable(CliffracerService):
    @listener("events.order", durable="orders-d", max_concurrency=1)
    async def on_order(self, subject: str, number: int = 0) -> None:
        _note_running()
        STATE["handled"].append(number)
        try:
            await asyncio.sleep(0.05)
        finally:
            STATE["running"] -= 1


def _delivery(number: int) -> tuple[Msg, list[str]]:
    sent: list[str] = []
    client = MagicMock()

    async def publish(subject, payload=b"", *args, **kwargs):
        sent.append("progress" if payload.startswith(b"+WPI") else payload.decode() or "ack")

    client.publish = AsyncMock(side_effect=publish)
    msg = Msg(
        _client=client,
        subject="events.order",
        reply=f"$JS.ACK.EVENTS.orders-d.1.{number + 1}.{number + 1}.1700000000000000000.0",
        data=json.dumps({"number": number}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return msg, sent


async def _durable_service() -> Durable:
    _reset_state()
    service = Durable(
        ServiceConfig(
            name="orders",
            health_port=0,
            jetstream_enabled=True,
            jetstream_ack_wait=0.4,
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.*"]),
            ],
        )
    )
    service.nc, service.js = AsyncMock(), AsyncMock()
    service.container.nc, service.container.js = service.nc, service.js
    await service.container._setup_extensions()
    service.container.discover_handlers()
    return service


async def test_a_jetstream_push_listener_runs_at_most_its_limit_with_no_service_limit():
    service = await _durable_service()
    callback = service.container.dispatcher.make_jetstream_event_callback("events.order")
    deliveries = [_delivery(n) for n in range(4)]
    started = time.monotonic()
    for msg, _ in deliveries:
        await asyncio.wait_for(callback(msg), 1.0)
    await _drain(service)

    assert sorted(STATE["handled"]) == [0, 1, 2, 3]
    assert STATE["most_running"] == 1
    assert all(sent and sent[-1] == "ack" for _, sent in deliveries)
    assert time.monotonic() - started >= 4 * 0.05 * 0.9


async def test_a_jetstream_pull_batch_runs_at_most_the_listeners_limit():
    service = await _durable_service()
    deliveries = [_delivery(n) for n in range(4)]
    sub = MagicMock()
    sub.fetch = AsyncMock(return_value=[msg for msg, _ in deliveries])

    await asyncio.wait_for(service.container._pull_once(sub, pattern="events.order"), NEVER)

    assert sorted(STATE["handled"]) == [0, 1, 2, 3]
    assert STATE["most_running"] == 1
    assert all(sent and sent[-1] == "ack" for _, sent in deliveries)


# --- a stopping service's refusals, on every path ------------------------------------------


async def test_a_fire_and_forget_request_holding_its_permits_while_the_service_stops_is_counted():
    service = _desk()
    dispatcher = service.container.dispatcher
    await dispatcher.on_async_request(_msg("single", kind="async"))
    await _until(lambda: service.ran == ["single"])
    await dispatcher.on_async_request(_msg("single", kind="async"))
    await _until(lambda: _limits(service)["single"]["waiting"] == 1)

    service.container.lifecycle._stop_requests += 1
    service.release.set()
    await _drain(service)

    assert service.ran == ["single"], "a request that waited was started by a stopping service"
    assert _limits(service)["single"]["refused"] == 1


class HeldDurable(CliffracerService):
    @listener("events.order", durable="orders-d", max_concurrency=1)
    async def on_order(self, subject: str, number: int = 0) -> None:
        STATE["handled"].append(number)
        await STATE["release"].wait()


async def test_a_jetstream_message_holding_its_permit_while_the_service_stops_is_counted():
    _reset_state()
    service = HeldDurable(
        ServiceConfig(
            name="orders",
            health_port=0,
            jetstream_enabled=True,
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.*"]),
            ],
        )
    )
    service.nc, service.js = AsyncMock(), AsyncMock()
    service.container.nc, service.container.js = service.nc, service.js
    await service.container._setup_extensions()
    service.container.discover_handlers()
    callback = service.container.dispatcher.make_jetstream_event_callback("events.order")
    (first, _), (second, second_sent) = _delivery(0), _delivery(1)
    await callback(first)
    await _until(lambda: STATE["handled"] == [0])
    await callback(second)
    await _until(lambda: _limits(service)["on_order"]["waiting"] == 1)

    service.container.lifecycle._stop_requests += 1
    STATE["release"].set()
    await _drain(service)

    assert STATE["handled"] == [0], "a message that waited was started by a stopping service"
    assert set(second_sent) <= {"progress"}, (
        f"a message left for redelivery was settled: {second_sent}"
    )
    assert _limits(service)["on_order"]["refused"] == 1


async def test_a_request_cancelled_between_its_two_waits_returns_the_methods_permit():
    """Holding the method's permit and waiting for the service's, a cancelled request gives the
    method's permit back, or the method's limit would shrink by one for every such cancel."""
    from cliffracer.core.dispatch.handler_limits import HandlerLimit, release, take

    class Signalling(asyncio.Semaphore):
        """A service semaphore that says when a request starts to wait for it."""

        def __init__(self) -> None:
            super().__init__(1)
            self.waited = asyncio.Event()

        async def acquire(self) -> bool:
            self.waited.set()  # the method's permit is held by now: take() asks for it first
            return await super().acquire()

    held = HandlerLimit("report", 1)
    service = Signalling()
    await asyncio.Semaphore.acquire(service)  # the service is full
    request = asyncio.create_task(take(held, service, None))
    await asyncio.wait_for(service.waited.wait(), NEVER)
    assert held.sem.locked(), "the request does not hold the method's permit"

    request.cancel()
    await asyncio.gather(request, return_exceptions=True)

    assert not held.sem.locked(), "the method's permit was kept"
    assert (held.in_flight, held.waiting) == (0, 0)
    assert await asyncio.wait_for(take(held, None, None), 1.0) is True, "the next request waited"
    release(held, None)


# --- on the event paths, too, the method's permit comes first ------------------------------
#
# Two service permits. Listener A is limited to one: A1 runs and holds one service permit, and A2
# waits at A's own limit holding nothing, so listener B's message gets the second service permit.
# Were the service's permit taken first, A2 would hold it while it waited, and B would wait too.


class TwoListeners(CliffracerService):
    @listener("events.a", fanout=True, max_concurrency=1)
    async def on_a(self, subject: str, number: int = 0) -> None:
        STATE["handled"].append(f"a{number}")
        await STATE["release"].wait()

    @listener("events.b", fanout=True)
    async def on_b(self, subject: str, number: int = 0) -> None:
        STATE["handled"].append(f"b{number}")


async def _b_runs(started) -> None:
    """Wait for listener B's message to be handled, naming what went missing when it is not."""
    try:
        await asyncio.wait_for(started, 1.0)
        await _until(lambda: "b1" in STATE["handled"])
    except TimeoutError:
        raise AssertionError(
            "listener B's message was not handled while A's waiting message held its place: "
            f"handled so far {STATE['handled']}"
        ) from None


def _event(subject: str, number: int) -> MockMessage:
    return MockMessage(subject=subject, data=json.dumps({"number": number}).encode(), headers={})


async def test_a_core_event_waiting_at_its_listeners_limit_holds_no_service_permit():
    _reset_state()
    service = TwoListeners(ServiceConfig(name="orders", health_port=0, max_event_concurrency=2))
    service._discover_handlers()
    on_a = service.container.dispatcher.make_event_callback("events.a")
    on_b = service.container.dispatcher.make_event_callback("events.b")

    await on_a(_event("events.a", 1))
    await _until(lambda: STATE["handled"] == ["a1"])
    waiting = asyncio.create_task(on_a(_event("events.a", 2)))  # A's callback waits for A
    await _until(lambda: _limits(service)["on_a"]["waiting"] == 1)
    try:
        await _b_runs(on_b(_event("events.b", 1)))
    finally:
        STATE["release"].set()
        await asyncio.wait_for(waiting, NEVER)
        await _drain(service)

    assert STATE["handled"][:2] == ["a1", "b1"], "B waited behind the message waiting at A"


class TwoDurables(CliffracerService):
    @listener("events.a", durable="a-d", max_concurrency=1)
    async def on_a(self, subject: str, number: int = 0) -> None:
        STATE["handled"].append(f"a{number}")
        await STATE["release"].wait()

    @listener("events.b", durable="b-d")
    async def on_b(self, subject: str, number: int = 0) -> None:
        STATE["handled"].append(f"b{number}")


def _subject_delivery(subject: str, durable: str, number: int) -> Msg:
    msg, _ = _delivery(number)
    msg.subject = subject
    msg.reply = f"$JS.ACK.EVENTS.{durable}.1.{number + 1}.{number + 1}.1700000000000000000.0"
    return msg


async def test_a_jetstream_message_waiting_at_its_listeners_limit_holds_no_service_permit():
    _reset_state()
    service = TwoDurables(
        ServiceConfig(
            name="orders",
            health_port=0,
            max_event_concurrency=2,
            jetstream_enabled=True,
            jetstream_ack_wait=30.0,
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.*"]),
            ],
        )
    )
    service.nc, service.js = AsyncMock(), AsyncMock()
    service.container.nc, service.container.js = service.nc, service.js
    await service.container._setup_extensions()
    service.container.discover_handlers()
    on_a = service.container.dispatcher.make_jetstream_event_callback("events.a")
    on_b = service.container.dispatcher.make_jetstream_event_callback("events.b")

    await on_a(_subject_delivery("events.a", "a-d", 1))
    await _until(lambda: STATE["handled"] == ["a1"])
    await on_a(_subject_delivery("events.a", "a-d", 2))
    await _until(lambda: _limits(service)["on_a"]["waiting"] == 1)
    try:
        await _b_runs(on_b(_subject_delivery("events.b", "b-d", 1)))
    finally:
        STATE["release"].set()
        await _drain(service)

    assert STATE["handled"][:2] == ["a1", "b1"], "B waited behind the message waiting at A"

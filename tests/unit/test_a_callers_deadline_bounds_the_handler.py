"""A caller's deadline travels with the request, bounds the handler, and is passed on.

A caller sends `Cliffracer-Timeout-Ms`, the whole milliseconds it still waits. The service bounds
the handler by the earlier of that and its own `max_rpc_processing_time`: a request already past
it is answered with code `deadline_exceeded` and not run, and a handler that runs past it is
cancelled and answered the same way. A call the handler makes waits at most what is left, sends
what is left, and is not sent when nothing is.

The handlers that would outlive a deadline wait on an event nothing sets, so a missing bound shows
as a test that hangs to its outer `wait_for`, not as a figure compared with a tight one. What is
asserted is the typed code, who set the deadline, and what ran.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, RpcDeadlineExceededError, ServiceConfig, rpc
from cliffracer.client import ServiceClient
from cliffracer.core import deadline as deadlines
from cliffracer.core.deadline import TIMEOUT_HEADER, Deadline, caller_budget
from cliffracer.core.exceptions import RpcServerError, RpcTimeoutError, raise_for_error_envelope
from cliffracer.testing import ServiceTestHarness
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

#: A budget the handlers below always outlive: they wait on an event nothing sets.
BUDGET_MS = "50"
#: The outer bound on a test whose handler is never cut off: a missing deadline fails here.
NEVER = 10.0


class Orders(CliffracerService):
    def __init__(self, config: ServiceConfig | None = None) -> None:
        super().__init__(config or ServiceConfig(name="orders", health_port=0))
        self.started = 0
        self.cancelled = 0
        self.release = asyncio.Event()
        self.seen: list[Deadline | None] = []

    @rpc
    async def stuck(self) -> int:
        self.started += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return 1

    @rpc
    async def swallows(self) -> str:
        self.started += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
        return "late"

    @rpc
    async def quick(self) -> int:
        self.seen.append(deadlines.current())
        return 7

    @rpc
    async def held(self) -> int:
        self.started += 1
        await self.release.wait()
        return 2


def _capped(seconds: float) -> ServiceConfig:
    return ServiceConfig(name="orders", health_port=0, max_rpc_processing_time=seconds)


async def _call(service: Orders, method: str, headers: dict[str, str] | None = None) -> dict:
    async with ServiceTestHarness(service) as harness:
        reply = await asyncio.wait_for(harness.rpc(method, headers=headers), NEVER)
    assert isinstance(reply.data, dict), reply.raw_data
    return reply.data


# --- the callee: a deadline bounds the handler -----------------------------------------------------


async def test_a_handler_past_its_callers_budget_is_cancelled_and_answered_deadline_exceeded():
    service = Orders()
    data = await _call(service, "stuck", {TIMEOUT_HEADER: BUDGET_MS})

    assert (data["code"], data["set_by"], data["budget"]) == ("deadline_exceeded", "caller", 0.05)
    assert data["success"] is False and data["error"].startswith("stuck exceeded its deadline")
    assert (service.started, service.cancelled) == (1, 1)


async def test_the_services_own_cap_bounds_a_request_whose_caller_sent_no_budget():
    service = Orders(_capped(0.05))
    data = await _call(service, "stuck")

    assert (data["code"], data["set_by"]) == ("deadline_exceeded", "service")
    assert service.cancelled == 1


@pytest.mark.parametrize(
    ("header_ms", "cap", "set_by"),
    [("50", 30.0, "caller"), ("30000", 0.05, "service")],
    ids=["the-callers-is-earlier", "the-caps-is-earlier"],
)
async def test_the_earlier_of_the_callers_budget_and_the_cap_is_the_deadline(
    header_ms, cap, set_by
):
    data = await _call(Orders(_capped(cap)), "stuck", {TIMEOUT_HEADER: header_ms})

    assert (data["code"], data["set_by"]) == ("deadline_exceeded", set_by)


async def test_a_handler_that_swallows_the_cancel_is_still_answered_deadline_exceeded():
    service = Orders()
    data = await _call(service, "swallows", {TIMEOUT_HEADER: BUDGET_MS})

    assert data["code"] == "deadline_exceeded"
    assert "result" not in data, "the late result of a handler cut off was sent"
    assert service.cancelled == 1


async def test_a_request_already_past_its_deadline_is_answered_without_running():
    service = Orders()
    async with ServiceTestHarness(service) as harness:
        loop = asyncio.get_running_loop()
        msg = MockMessage(subject="orders.rpc.stuck", data=b"{}", headers={})
        spent = Deadline(loop.time() - 0.01, 0.05, "caller")
        await harness.container.dispatcher.rpc.handle_rpc_request(msg, deadline=spent)
    data = json.loads(msg.responded_data or b"null")

    assert data["code"] == "deadline_exceeded"
    assert data["error"].startswith("stuck was not started")
    assert service.started == 0


async def test_a_request_whose_budget_ends_while_it_waits_for_a_permit_is_not_run():
    service = Orders(ServiceConfig(name="orders", health_port=0, max_rpc_concurrency=1))
    async with ServiceTestHarness(service) as harness:
        rpc_dispatch = harness.container.dispatcher.rpc
        holder = MockMessage(subject="orders.rpc.held", data=b"{}", headers={})
        waiter = MockMessage(
            subject="orders.rpc.stuck", data=b"{}", headers={TIMEOUT_HEADER: BUDGET_MS}
        )
        await rpc_dispatch.on_rpc_request(holder)  # takes the one permit and keeps it
        await asyncio.wait_for(rpc_dispatch.on_rpc_request(waiter), NEVER)
        await asyncio.wait_for(_until(lambda: waiter.responded_data is not None), NEVER)
        service.release.set()
        await asyncio.wait_for(_until(lambda: holder.responded_data is not None), NEVER)

    assert json.loads(waiter.responded_data or b"null")["code"] == "deadline_exceeded"
    assert service.started == 1, "the request that ran out of time waiting was started anyway"
    assert json.loads(holder.responded_data or b"null")["result"] == 2


async def test_a_fire_and_forget_handler_is_bounded_by_the_cap_alone():
    service = Orders(_capped(0.05))
    async with ServiceTestHarness(service) as harness:
        msg = MockMessage(
            subject="orders.async.stuck", data=b"{}", headers={TIMEOUT_HEADER: "30000"}, reply=""
        )
        await asyncio.wait_for(harness.container.dispatcher.rpc.handle_async_request(msg), NEVER)

    assert service.cancelled == 1


async def test_a_handler_sees_its_requests_deadline_and_finishes_within_it():
    service = Orders(_capped(30.0))
    data = await _call(service, "quick", {TIMEOUT_HEADER: "20000"})

    assert data["result"] == 7
    (seen,) = service.seen
    assert seen is not None and (seen.budget, seen.set_by) == (20.0, "caller")


async def test_CONTROL_with_no_budget_and_no_cap_the_handler_runs_to_its_end_and_is_answered():
    """Today's shape, kept: unbounded, the reply is sent whenever the handler returns."""
    service = Orders()
    async with ServiceTestHarness(service) as harness:
        call = asyncio.create_task(harness.rpc("held"))
        await asyncio.wait_for(_until(lambda: service.started == 1), NEVER)
        await asyncio.sleep(0.1)  # longer than every budget above
        assert not call.done(), "a request with no budget was cut off"
        service.release.set()
        reply = await asyncio.wait_for(call, NEVER)

    assert reply.data["result"] == 2


# --- the header ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("1500", 1.5),
        (" 1 ", 0.001),
        ("86400000", 86400.0),
        ("", None),
        ("0", None),
        ("-5", None),
        ("1.5", None),
        ("1e3", None),
        ("٣", None),
        ("86400001", None),
        ("fast", None),
    ],
)
def test_only_a_whole_number_of_milliseconds_up_to_a_day_is_a_budget(value, seconds):
    assert caller_budget({TIMEOUT_HEADER: value}) == seconds
    assert caller_budget({TIMEOUT_HEADER.lower(): value}) == seconds


async def test_a_deadline_is_current_only_inside_its_scope():
    loop = asyncio.get_running_loop()
    deadline = Deadline(loop.time() + 1.0, 1.0, "caller")
    assert deadlines.current() is None
    with deadlines.scoped(deadline):
        assert deadlines.current() is deadline
    assert deadlines.current() is None, "the scope left its deadline behind"


def test_no_header_is_no_budget():
    assert caller_budget({"Content-Type": "application/json"}) is None


# --- the caller: what a call sends and waits ------------------------------------------------------


class Wire:
    """A connection that records each request's timeout and headers."""

    is_closed = False

    def __init__(self) -> None:
        self.sent: list[tuple[float, dict]] = []

    async def request(self, subject, payload, timeout=None, headers=None):
        self.sent.append((timeout, dict(headers or {})))
        reply = AsyncMock()
        reply.data = b'{"success": true, "result": 1}'
        reply.headers = None
        return reply


def _calling_service() -> tuple[CliffracerService, Wire]:
    service = CliffracerService(ServiceConfig(name="caller", health_port=0, request_timeout=30))
    wire = Wire()
    service.nc = wire  # type: ignore[assignment]
    return service, wire


async def _within(seconds: float, call):
    loop = asyncio.get_running_loop()
    with deadlines.scoped(Deadline(loop.time() + seconds, seconds, "caller")):
        return await call()


async def test_a_call_with_no_deadline_waits_and_sends_its_own_timeout():
    service, wire = _calling_service()
    await service.call_rpc("billing", "charge")

    ((timeout, headers),) = wire.sent
    assert (timeout, headers[TIMEOUT_HEADER]) == (30, "30000")


async def test_a_call_inside_a_handler_waits_and_sends_at_most_what_its_request_has_left():
    service, wire = _calling_service()
    await _within(2.0, lambda: service.call_rpc("billing", "charge"))

    ((timeout, headers),) = wire.sent
    assert 0 < timeout <= 2.0
    assert 0 < int(headers[TIMEOUT_HEADER]) <= 2000


async def test_a_call_whose_request_has_no_time_left_is_not_sent():
    service, wire = _calling_service()
    with pytest.raises(RpcTimeoutError, match="was not sent"):
        await _within(-1.0, lambda: service.call_rpc("billing", "charge"))
    assert wire.sent == []


async def test_a_service_client_inside_a_handler_sends_what_is_left_and_nothing_when_it_is_spent():
    wire = Wire()
    client = ServiceClient(wire, service="billing", timeout=30, verify=False)  # type: ignore[arg-type]
    await _within(2.0, lambda: client._request("billing.rpc.charge", b"{}"))
    with pytest.raises(RpcTimeoutError, match="was not sent"):
        await _within(-1.0, lambda: client._request("billing.rpc.charge", b"{}"))

    ((timeout, headers),) = wire.sent
    assert 0 < timeout <= 2.0 and 0 < int(headers[TIMEOUT_HEADER]) <= 2000


async def test_a_call_async_sends_no_budget():
    service, _ = _calling_service()
    published: list[dict] = []

    async def publish(subject, payload, headers=None):
        published.append(dict(headers or {}))

    service.nc.publish = publish  # type: ignore[union-attr]
    await _within(2.0, lambda: service.call_async("billing", "charge"))

    (headers,) = published
    assert TIMEOUT_HEADER not in headers


# --- the reply's code ------------------------------------------------------------------------------


def test_deadline_exceeded_is_raised_as_a_timeout_naming_the_budget_and_who_set_it():
    reply = {
        "success": False,
        "error": "stuck exceeded its deadline of 0.050s (set by its caller) and was cancelled",
        "code": "deadline_exceeded",
        "budget": 0.05,
        "elapsed": 0.051,
        "set_by": "caller",
    }
    with pytest.raises(RpcDeadlineExceededError) as caught:
        raise_for_error_envelope(reply, "orders.rpc.stuck")

    error = caught.value
    assert isinstance(error, RpcTimeoutError) and isinstance(error, TimeoutError)
    assert (error.budget, error.elapsed, error.set_by) == (0.05, 0.051, "caller")


def test_CONTROL_an_unknown_code_is_still_a_server_error():
    with pytest.raises(RpcServerError):
        raise_for_error_envelope({"error": "x", "code": "not_a_code"}, "orders.rpc.stuck")


async def _until(condition) -> None:
    while not condition():
        await asyncio.sleep(0.005)


async def test_a_handler_that_keeps_running_past_its_deadline_is_named_at_twice_its_budget():
    """One that catches the cancellation and carries on is named while it still runs."""
    from loguru import logger

    release = asyncio.Event()

    class Stubborn(CliffracerService):
        @rpc
        async def stubborn(self) -> int:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()  # it does not stop
            return 1

    loop = asyncio.get_running_loop()
    lines: list[tuple[float, str]] = []
    sink = logger.add(lambda message: lines.append((loop.time(), str(message))), level="WARNING")
    try:
        async with ServiceTestHarness(Stubborn(ServiceConfig(name="orders", health_port=0))) as h:
            began = loop.time()
            call = asyncio.create_task(h.rpc("stubborn", headers={TIMEOUT_HEADER: BUDGET_MS}))

            async def named() -> None:
                while not any("still running" in line for _, line in lines):
                    await asyncio.sleep(0.005)

            try:
                await asyncio.wait_for(named(), NEVER)
            except TimeoutError:
                raise AssertionError(
                    f"no warning named the handler still running {NEVER}s after it began"
                ) from None
            assert not call.done(), "the handler stopped before it was named"
            release.set()
            await asyncio.wait_for(call, NEVER)
    finally:
        release.set()
        logger.remove(sink)

    ((at, line),) = [(at, line) for at, line in lines if "still running" in line]
    assert "stubborn is still running 0.050s after being cancelled at its deadline" in line
    # Set for the deadline plus the budget again: never sooner than twice the budget.
    assert at - began >= 2 * 0.05, f"the warning came {at - began:.3f}s after the request"

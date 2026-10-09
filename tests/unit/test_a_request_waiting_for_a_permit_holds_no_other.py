"""A request waiting for a concurrency permit holds no other request, and admission is bounded.

Every method's requests arrive on one subscription, and nats-py runs a subscription's callbacks
one at a time, so a callback that waited for a permit held every later request, for every method,
in the client's queue. The callback now fixes the request's deadline, admits it, spawns its task
and returns; the task waits for the permit. `max_rpc_in_flight` bounds how many are admitted at
once, and a request over it is answered with code `busy`. Unset, nothing is refused: before the
callback, nats-py's own pending limit (524288 messages by default) is what a burst meets, as it
always was.

A request that gets its permit while the service is stopping is answered `busy` and not started.
"""

import asyncio
import json
from typing import Any

import pytest
from loguru import logger

from cliffracer import CliffracerService, RpcBusyError, ServiceConfig, rpc
from cliffracer.core.exceptions import raise_for_error_envelope
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

#: The outer bound on a wait that only a defect can make long.
NEVER = 10.0


class Desk(CliffracerService):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.release = asyncio.Event()
        self.ran: list[str] = []

    @rpc
    async def held(self) -> int:
        self.ran.append("held")
        await self.release.wait()
        return 1

    @rpc
    async def quick(self) -> int:
        self.ran.append("quick")
        return 2


def _desk(**config: Any) -> Desk:
    service = Desk(ServiceConfig(name="desk", health_port=0, **config))
    service._discover_handlers()
    service.container.lifecycle._running = True
    return service


def _msg(method: str, subject_prefix: str = "desk.rpc") -> MockMessage:
    return MockMessage(subject=f"{subject_prefix}.{method}", data=b"{}", headers={})


def _reply(msg: MockMessage) -> dict | None:
    return None if msg.responded_data is None else json.loads(msg.responded_data)


async def _drain(service: Desk) -> None:
    tasks = list(service.container._active_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), NEVER)


async def _until(condition) -> None:
    while not condition():
        await asyncio.sleep(0.005)


async def test_a_request_waiting_for_a_permit_does_not_hold_another_methods_request():
    service = _desk(max_rpc_concurrency=1)
    on_request = service.container.dispatcher.on_rpc_request
    holder, waiting, other = _msg("held"), _msg("held"), _msg("quick")

    await on_request(holder)
    await asyncio.wait_for(_until(lambda: service.ran == ["held"]), NEVER)
    # Neither callback may wait: the permit is held, and nothing sets `release` until after both.
    await asyncio.wait_for(on_request(waiting), 1.0)
    await asyncio.wait_for(on_request(other), 1.0)
    service.release.set()
    await _drain(service)

    assert sorted(service.ran) == ["held", "held", "quick"]
    assert [(_reply(m) or {}).get("result") for m in (holder, waiting, other)] == [1, 1, 2]


async def _burst(service: Desk, size: int) -> list[dict | None]:
    msgs = [_msg("held") for _ in range(size)]
    for msg in msgs:
        await asyncio.wait_for(service.container.dispatcher.on_rpc_request(msg), 1.0)
    await asyncio.sleep(0)  # the refusals' own reply tasks run
    service.release.set()
    await _drain(service)
    return [_reply(m) for m in msgs]


async def test_with_no_admission_bound_a_burst_over_the_concurrency_limit_all_finishes():
    replies = await _burst(_desk(max_rpc_concurrency=4), 50)

    assert sum((r or {}).get("code") == "busy" for r in replies) == 0
    assert [(r or {}).get("result") for r in replies] == [1] * 50


async def test_max_rpc_in_flight_answers_what_is_over_it_busy_and_runs_the_rest():
    service = _desk(max_rpc_concurrency=4, max_rpc_in_flight=32)
    replies = await _burst(service, 50)

    codes = [(r or {}).get("code") for r in replies]
    assert codes.count("busy") == 18
    assert [(r or {}).get("result") for r in replies[:32]] == [1] * 32
    first_refused = replies[32]
    assert first_refused is not None
    assert (first_refused["limit"], first_refused["in_flight"]) == (32, 32)
    assert service.ran.count("held") == 32


async def test_a_permit_that_comes_while_the_service_is_stopping_does_not_start_the_handler():
    service = _desk(max_rpc_concurrency=1)
    holder, waiting = _msg("held"), _msg("quick")
    await service.container.dispatcher.on_rpc_request(holder)
    await asyncio.wait_for(_until(lambda: service.ran == ["held"]), NEVER)
    await service.container.dispatcher.on_rpc_request(waiting)

    # What `stop()` records first, and what the container tells its dispatchers through: read
    # here through the container's own wiring, not a stand-in set on the dispatcher.
    service.container.lifecycle._stop_requests += 1
    assert service.container.lifecycle.stop_requested
    service.release.set()
    await _drain(service)

    reply = _reply(waiting)
    assert reply is not None and reply["code"] == "busy"
    assert reply["error"] == "quick not started: the service is stopping"
    assert "quick" not in service.ran


async def test_a_task_cancelled_before_it_runs_gives_back_its_admission():
    service = _desk(max_rpc_concurrency=1, max_rpc_in_flight=1)
    before = set(asyncio.all_tasks())
    await service.container.dispatcher.on_rpc_request(_msg("quick"))
    (spawned,) = [t for t in asyncio.all_tasks() - before if not t.done()]
    spawned.cancel()
    await asyncio.gather(spawned, return_exceptions=True)
    await asyncio.sleep(0)  # done-callbacks run on the next loop iteration

    after = _msg("quick")
    await service.container.dispatcher.on_rpc_request(after)
    await _drain(service)
    assert (_reply(after) or {}).get("result") == 2, "the cancelled request kept its admission"


async def test_a_fire_and_forget_request_over_the_bound_is_dropped_and_logged():
    service = _desk(max_rpc_concurrency=1, max_rpc_in_flight=1)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        on_async = service.container.dispatcher.on_async_request
        await asyncio.wait_for(on_async(_msg("held", "desk.async")), 1.0)
        await asyncio.wait_for(_until(lambda: service.ran == ["held"]), NEVER)
        await asyncio.wait_for(on_async(_msg("quick", "desk.async")), 1.0)
        service.release.set()
        await _drain(service)
    finally:
        logger.remove(sink)

    assert service.ran == ["held"]
    assert any(line.startswith("quick not admitted") for line in lines), lines


def test_busy_is_raised_as_rpc_busy_error_with_its_counts():
    with pytest.raises(RpcBusyError) as caught:
        raise_for_error_envelope(
            {"error": "held not admitted", "code": "busy", "limit": 32, "in_flight": 32},
            "desk.rpc.held",
        )
    assert (caught.value.limit, caught.value.in_flight) == (32, 32)


def test_a_busy_reply_for_a_stopping_service_carries_no_counts():
    with pytest.raises(RpcBusyError) as caught:
        raise_for_error_envelope(
            {"error": "quick not started: the service is stopping", "code": "busy"},
            "desk.rpc.quick",
        )
    assert (caught.value.limit, caught.value.in_flight) == (None, None)

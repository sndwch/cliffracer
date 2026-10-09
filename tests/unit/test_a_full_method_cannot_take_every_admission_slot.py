"""Requests waiting at a full method cannot take every admission slot and starve other methods.

`max_rpc_in_flight` admits a request in the delivery callback, and the request keeps its slot while
it waits for its method's `max_concurrency` permit. `max_queued=` bounds how many may wait at the
method: a request that finds `limit + max_queued` of its method's requests already admitted is
answered `busy`, naming the method, counted refused, and takes no admission slot. Unset, with an
admission bound, it is half that bound (at least 1), so the others always keep a slot; with no
admission bound there is nothing to starve, and no cap.
"""

import asyncio
import json
from typing import Any

import pytest

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
from cliffracer.core.dispatch import default_max_queued
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

NEVER = 10.0


# --- the declaration and the default -------------------------------------------------------


@pytest.mark.parametrize(
    ("bound", "default"), [(None, None), (1, 1), (2, 1), (4, 2), (5, 2), (32, 16)]
)
def test_the_default_is_half_the_admission_bound_at_least_one_and_none_without_one(bound, default):
    assert default_max_queued(bound) == default


def test_max_queued_is_recorded_on_the_method_and_zero_is_a_queue_of_none():
    async def f(self) -> None: ...

    assert rpc(max_concurrency=1, max_queued=0)(f)._cliffracer_max_queued == 0

    async def g(self) -> None: ...

    assert async_rpc(max_concurrency=2, max_queued=5)(g)._cliffracer_max_queued == 5


@pytest.mark.parametrize("bad", [-1, True, 1.5, "2"])
def test_a_max_queued_that_is_not_an_int_of_zero_or_more_is_refused(bad):
    async def f(self) -> None: ...

    with pytest.raises(ConfigurationError, match="max_queued=.*an int of 0 or more"):
        rpc(max_concurrency=1, max_queued=bad)(f)


def test_max_queued_without_max_concurrency_is_refused():
    async def f(self) -> None: ...

    with pytest.raises(ConfigurationError, match="without max_concurrency"):
        rpc(max_queued=3)(f)


def test_one_method_has_one_queue_across_its_decorators():
    async def f(self) -> None: ...

    with pytest.raises(ConfigurationError, match="another decorator on the same method gave 2"):
        async_rpc(max_concurrency=1, max_queued=3)(rpc(max_concurrency=1, max_queued=2)(f))


def test_the_listener_decorators_refuse_max_queued_by_name():
    from pydantic import BaseModel

    class Model(BaseModel):
        n: int = 0

    for build, name in (
        (lambda: listener("x.y", fanout=True, max_concurrency=1, max_queued=2), "listener"),
        (
            lambda: validated_listener("x.y", Model, fanout=True, max_concurrency=1, max_queued=2),
            "validated_listener",
        ),
        (lambda: broadcast("x.y", max_concurrency=1, max_queued=2), "broadcast"),
    ):
        with pytest.raises(
            ConfigurationError, match=rf"@{name} was given max_queued=2; .*requests"
        ):
            build()


# --- the starvation and its bound ----------------------------------------------------------


class Desk(CliffracerService):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.release = asyncio.Event()

    @rpc(max_concurrency=1)
    async def slow(self) -> int:
        await self.release.wait()
        return 1

    @rpc(max_concurrency=1, max_queued=9)
    async def slow_unbounded(self) -> int:
        await self.release.wait()
        return 1

    @rpc
    async def fast(self) -> int:
        return 2


def _desk(**config: Any) -> Desk:
    service = Desk(ServiceConfig(name="desk", health_port=0, **config))
    service._discover_handlers()
    service.container.lifecycle._running = True
    return service


def _msg(method: str, kind: str = "rpc") -> MockMessage:
    return MockMessage(subject=f"desk.{kind}.{method}", data=b"{}", headers={})


def _reply(msg: MockMessage) -> dict | None:
    return None if msg.responded_data is None else json.loads(msg.responded_data)


async def _burst(service: Desk, method: str, size: int) -> list[MockMessage]:
    msgs = [_msg(method) for _ in range(size)]
    for msg in msgs:
        await asyncio.wait_for(service.container.dispatcher.on_rpc_request(msg), 1.0)
    await asyncio.sleep(0)  # the refusals' own reply tasks run
    return msgs


async def _drain(service: Desk) -> None:
    service.release.set()
    tasks = list(service.container._active_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), NEVER)


async def test_with_the_default_cap_a_full_method_leaves_slots_for_the_others():
    service = _desk(max_rpc_in_flight=4)  # the default max_queued is 2
    slow = await _burst(service, "slow", 10)
    fast = []
    admitted = service.container.dispatcher.rpc._admitted
    for _ in range(3):  # one at a time: the slot the slow method leaves is one slot
        fast.extend(await _burst(service, "fast", 1))
        await asyncio.wait_for(_until(lambda: admitted["rpc"] == 3), NEVER)  # its task is done

    assert [(_reply(m) or {}).get("result") for m in fast] == [2, 2, 2]
    refused = [_reply(m) for m in slow[3:]]
    assert {(r or {}).get("code") for r in refused} == {"busy"}
    first = refused[0] or {}
    assert (
        first["error"] == "slow not admitted: 1 running and 2 waiting, the most this method takes"
    )
    assert (first["limit"], first["in_flight"]) == (3, 3)
    assert service.container.dispatcher.limits.details()["slow"] == {
        "limit": 1,
        "max_queued": 2,
        "in_flight": 1,
        "waiting": 2,
        "refused": 7,
    }
    await _drain(service)
    assert [(_reply(m) or {}).get("result") for m in slow[:3]] == [1, 1, 1]


async def test_CONTROL_a_queue_as_deep_as_the_admission_bound_starves_the_others_again():
    service = _desk(max_rpc_in_flight=4)
    await _burst(service, "slow_unbounded", 10)
    fast = await _burst(service, "fast", 3)

    assert {(_reply(m) or {}).get("code") for m in fast} == {"busy"}
    assert "fast not admitted: 4 requests are already in flight" in (_reply(fast[0]) or {})["error"]
    await _drain(service)


async def test_a_finished_request_gives_its_method_slot_back():
    """Limit + max_queued + 3 calls in turn are all answered: the count returns as each ends."""
    service = _desk(max_rpc_in_flight=4)  # `slow` holds 1 running and the default 2 waiting
    service.release.set()  # each call returns at once
    limits = service.container.dispatcher.limits
    admitted = service.container.dispatcher.rpc._admitted

    replies = []
    for _ in range(1 + 2 + 3):
        (msg,) = await _burst(service, "slow", 1)
        await asyncio.wait_for(_until(lambda: admitted["rpc"] == 0), NEVER)  # its task is done
        replies.append(_reply(msg))
        between = limits.details()["slow"]
        assert (between["in_flight"], between["waiting"]) == (0, 0), between

    assert [(r or {}).get("result") for r in replies] == [1] * 6, replies
    assert limits.details()["slow"]["refused"] == 0


async def test_a_request_refused_at_its_methods_queue_takes_no_admission_slot():
    service = _desk(max_rpc_in_flight=4)
    await _burst(service, "slow", 10)

    assert service.container.dispatcher.rpc._admitted["rpc"] == 3
    await _drain(service)
    assert service.container.dispatcher.rpc._admitted["rpc"] == 0
    assert service.container.dispatcher.limits.details()["slow"]["refused"] == 7


async def test_with_no_admission_bound_there_is_no_default_cap():
    service = _desk()
    slow = await _burst(service, "slow", 10)

    assert all(m.responded_data is None for m in slow), "a request was refused"
    assert service.container.dispatcher.limits.details()["slow"]["max_queued"] is None
    await _drain(service)
    assert [(_reply(m) or {}).get("result") for m in slow] == [1] * 10


async def test_a_fire_and_forget_request_over_the_queue_is_dropped_and_takes_no_slot():
    service = _desk(max_rpc_in_flight=4)
    for _ in range(5):
        await service.container.dispatcher.on_async_request(_msg("slow", kind="async"))

    rpc_dispatcher = service.container.dispatcher.rpc
    assert rpc_dispatcher._admitted["async"] == 3
    assert service.container.dispatcher.limits.details()["slow"]["refused"] == 2
    await _drain(service)


async def test_health_shows_each_methods_max_queued():
    service = Desk(ServiceConfig(name="desk", health_port=0, max_rpc_in_flight=6))
    service._discover_handlers()

    limits = (await service.health_check())["handler_limits"]

    assert (limits["slow"]["max_queued"], limits["slow_unbounded"]["max_queued"]) == (3, 9)


async def _until(condition) -> None:
    while not condition():
        await asyncio.sleep(0.005)

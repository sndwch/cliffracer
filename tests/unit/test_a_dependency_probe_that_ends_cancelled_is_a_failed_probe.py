"""A probe that ends cancelled is reported as a failed dependency, and `/ready` still answers.

`dependencies.py` promises that "a broken check must not take the endpoint down". A probe awaiting a
future its driver cancels (a pool closing under a waiting `acquire`, a client dropping a pending
request on reconnect) ends as a cancelled task, and `task.result()` raised `CancelledError` in the
`except` arm written for "the caller is going away". It left `check_dependencies`, and the health
listener answered nothing: the connection closed with no response, where ADR-0003 says 503 and the
body says which dependency is down.

A cancellation of the caller itself is still not swallowed.
"""

import asyncio
import json

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.dependencies import Dependency, _run_one, check_dependencies

pytestmark = pytest.mark.unit


async def a_pool_closed_under_the_probe():
    """What a driver does to a waiter when its pool closes: cancel the future the probe awaits."""
    waiter = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_later(0.01, waiter.cancel)
    await waiter


async def a_healthy_probe():
    return None


async def a_probe_that_raises():
    raise ConnectionError("db unreachable")


async def test_a_probe_that_ends_cancelled_is_reported_failed_not_raised():
    results = await check_dependencies([Dependency("db", a_pool_closed_under_the_probe)])

    assert results["db"]["ok"] is False
    assert results["db"]["error"]


async def test_one_cancelled_probe_does_not_take_the_other_dependencies_down_with_it():
    results = await check_dependencies(
        [Dependency("db", a_pool_closed_under_the_probe), Dependency("cache", a_healthy_probe)]
    )

    assert results["db"]["ok"] is False
    assert results["cache"]["ok"] is True


async def test_the_ready_endpoint_answers_503_naming_the_dependency():
    service = CliffracerService(ServiceConfig(name="probe", health_port=0))
    service.add_dependency("db", a_pool_closed_under_the_probe)
    service._running = True
    await service.health_listener.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", service.health_listener.port)
        writer.write(b"GET /ready HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 5)
        writer.close()
    finally:
        await service.health_listener.stop()

    assert raw, "the connection was closed with no response"
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.split(b"\r\n")[0].split()[1] == b"503"
    assert json.loads(body)["dependencies"]["db"]["ok"] is False


async def test_CONTROL_a_probe_that_raises_is_still_a_failed_probe():
    results = await check_dependencies([Dependency("db", a_probe_that_raises)])

    assert results["db"]["ok"] is False


async def test_CONTROL_a_probe_that_answers_is_still_ok():
    results = await check_dependencies([Dependency("cache", a_healthy_probe)])

    assert results["cache"]["ok"] is True


async def test_CONTROL_cancelling_the_caller_is_still_raised_to_the_caller():
    """Not swallowed: a caller that is going away must see it, and the probe goes with it."""
    started = asyncio.Event()
    probe_cancelled = asyncio.Event()

    async def slow_probe():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            probe_cancelled.set()
            raise

    task = asyncio.create_task(check_dependencies([Dependency("db", slow_probe, timeout=60)]))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(probe_cancelled.wait(), 1)


async def test_CONTROL_one_probe_run_re_raises_its_callers_cancellation_itself():
    """`gather` would raise the caller's cancellation even if `_run_one` swallowed it, so this reads
    the function the `except` arm is in, not what wraps it."""
    started = asyncio.Event()

    async def slow_probe():
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(_run_one(Dependency("db", slow_probe, timeout=60)))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

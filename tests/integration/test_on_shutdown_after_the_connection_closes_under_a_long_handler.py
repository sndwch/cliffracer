"""A service whose broker connection closes while a handler is still running gets its `on_shutdown`.

The measured shape of the defect: the closed-connection callback gives the stop it starts a fixed
10 seconds. A handler that outlasts that keeps the drain going, the callback cancels the stop, and
the cancel used to jump past `on_shutdown`, which then never ran, for this stop or any later one,
although `shutdown_timeout` allowed 30 seconds. It now runs, once, right after the cut-off.

This is the real closed callback, driven by closing the service's own connection, on the suite's
broker. The cut-off is patched to one second; the handler's sleep and the bounds on the close are
relative to it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core import connection

pytestmark = [pytest.mark.integration, pytest.mark.nats_required, pytest.mark.slow]

CUTOFF = 1.0


async def _until(condition, what: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.05)


async def test_on_shutdown_runs_once_when_the_connection_closes_under_a_handler_that_outlasts_the_cutoff(
    monkeypatch,
):
    monkeypatch.setattr(connection, "_CLOSED_STOP_TIMEOUT", CUTOFF)
    ran: list[float] = []

    class Slow(CliffracerService):
        @rpc
        async def linger(self) -> str:
            await asyncio.sleep(connection._CLOSED_STOP_TIMEOUT + 6)
            return "done"

        async def on_shutdown(self) -> None:
            ran.append(time.monotonic())

    svc = Slow(ServiceConfig(name="closed_live", health_port=0, shutdown_timeout=30.0))
    caller = CliffracerService(ServiceConfig(name="closed_live_caller", health_port=0))
    await svc.start()
    await caller.start()
    request = asyncio.create_task(caller.call_rpc("closed_live", "linger"))
    try:
        await _until(lambda: len(svc.container.lifecycle.active_tasks) >= 1, "the handler", 5)

        closed_at = time.monotonic()
        await svc.nc.close()
        await _until(lambda: bool(ran), "on_shutdown", connection._CLOSED_STOP_TIMEOUT + 10)

        elapsed = ran[0] - closed_at
        # Upper bound. CI p99 1 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 1 s,
        # 2243x the overshoot; below 30 s (shutdown_timeout=30.0).
        # Lower bound: the cutoff less 0.5 s; an on_shutdown run at the close itself falls under it.
        # Load can only lengthen it.
        assert (
            connection._CLOSED_STOP_TIMEOUT - 0.5 <= elapsed < connection._CLOSED_STOP_TIMEOUT + 5
        ), f"on_shutdown ran {elapsed:.1f}s after the close"
        await asyncio.sleep(0.5)
        await svc.stop()
        assert len(ran) == 1, f"on_shutdown ran {len(ran)} times"
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await caller.stop()

"""When the broker closes for good and the stop it starts is cut off, the log says why and does not claim success.

The closed-connection callback gives the service a fixed time to stop in. A `TimeoutError` has no
message, so the warning that formatted it read "could not stop cleanly on connection close: " with
nothing after the colon, and the line after it said the service "stopped after NATS connection
closed" whether it had or not. An operator reading those two lines learned neither that the stop was
cut off, nor how long it had been given, nor that steps after the one it was in may not have run.
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core import connection

pytestmark = pytest.mark.unit

STOPPED = "stopped after NATS connection closed"
COULD_NOT = "could not stop cleanly on connection close"


class _ClosedNats:
    is_connected = False
    is_closed = True


def _service() -> CliffracerService:
    svc = CliffracerService(ServiceConfig(name="closed-probe"))
    svc._running = True
    svc.nc = _ClosedNats()
    return svc


async def _closed_with(svc: CliffracerService, stop) -> list[tuple[str, str]]:
    svc.container.connection.on_closed_handler = stop
    seen: list[tuple[str, str]] = []
    sink = logger.add(lambda m: seen.append((m.record["level"].name, m.record["message"])))
    try:
        await asyncio.wait_for(svc.container.connection._closed_callback(), timeout=5)
    finally:
        logger.remove(sink)
    return seen


async def test_a_stop_that_is_cut_off_says_how_long_it_was_given_and_what_may_not_have_run(
    monkeypatch,
):
    monkeypatch.setattr(connection, "_CLOSED_STOP_TIMEOUT", 0.05)

    async def wedged() -> None:
        await asyncio.Event().wait()

    seen = await _closed_with(_service(), wedged)

    (warning,) = [m for level, m in seen if level == "WARNING" and COULD_NOT in m]
    assert "0.05 seconds" in warning
    assert "shutdown_timeout" in warning and "on_shutdown" in warning
    assert not warning.rstrip().endswith(":"), "the warning ends on a colon with no reason"


async def test_a_stop_that_is_cut_off_does_not_log_that_the_service_stopped(monkeypatch):
    monkeypatch.setattr(connection, "_CLOSED_STOP_TIMEOUT", 0.05)

    async def wedged() -> None:
        await asyncio.Event().wait()

    seen = await _closed_with(_service(), wedged)

    assert not any(STOPPED in m for _, m in seen), seen


async def test_a_stop_that_raises_names_the_exception_and_does_not_log_that_it_stopped():
    async def broken() -> None:
        raise RuntimeError("teardown wedged")

    seen = await _closed_with(_service(), broken)

    warnings = [m for level, m in seen if level == "WARNING" and COULD_NOT in m]
    assert len(warnings) == 1 and "RuntimeError('teardown wedged')" in warnings[0]
    assert not any(STOPPED in m for _, m in seen), seen


async def test_a_stop_that_raises_an_exception_with_no_message_is_still_named():
    async def silent() -> None:
        raise ValueError

    seen = await _closed_with(_service(), silent)

    (warning,) = [m for level, m in seen if level == "WARNING" and COULD_NOT in m]
    assert "ValueError()" in warning


async def test_CONTROL_a_stop_that_finishes_logs_that_the_service_stopped_and_warns_of_nothing():
    stopped: list[bool] = []

    async def clean() -> None:
        stopped.append(True)

    seen = await _closed_with(_service(), clean)

    assert stopped == [True]
    assert any(level == "ERROR" and STOPPED in m for level, m in seen), seen
    assert not any(COULD_NOT in m for _, m in seen), seen

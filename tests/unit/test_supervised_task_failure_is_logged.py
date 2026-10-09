"""A crashed background task is logged with its message and its traceback.

The failure is read from the log records the framework writes, with no drain or
service stop in between: a drain retrieves the exception itself, so a test that
shuts the service down cannot tell whether the completion callback did.
"""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Iterator
from typing import Any

import pytest
from loguru import logger

from cliffracer import ServiceConfig
from cliffracer.core.lifecycle import LifecycleManager
from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit

BRACED = "bad payload {user_id} not in {'a': 1}"


class Captured:
    def __init__(self) -> None:
        self.records: list[Any] = []
        self.loop_errors: list[dict[str, Any]] = []

    def errors(self, containing: str) -> list[Any]:
        return [
            r for r in self.records if r["level"].name == "ERROR" and containing in r["message"]
        ]

    def traceback_text(self, record: Any) -> str:
        exc = record["exception"]
        if exc is None:
            return ""
        return "".join(traceback.format_exception(exc.type, exc.value, exc.traceback))


@pytest.fixture
async def captured() -> Iterator[Captured]:
    seen = Captured()
    logger.remove()
    sink_id = logger.add(lambda message: seen.records.append(message.record), level="DEBUG")
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: seen.loop_errors.append(context))
    yield seen
    loop.set_exception_handler(previous)
    logger.remove(sink_id)


async def _crash(exc: BaseException) -> None:
    raise exc


def _lifecycle() -> LifecycleManager:
    return LifecycleManager(ServiceConfig(name="crash_logging", health_listener=False))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["ordinary failure", BRACED], ids=["plain", "braces"])
async def test_a_crashed_supervised_task_is_logged_with_its_message(captured, text):
    _lifecycle().spawn_supervised_task(_crash(ValueError(text)), name="boom")
    await asyncio.sleep(0.05)

    (record,) = captured.errors("background task 'boom'")
    assert text in record["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["ordinary failure", BRACED], ids=["plain", "braces"])
async def test_a_crashed_supervised_task_is_logged_with_its_traceback(captured, text):
    _lifecycle().spawn_supervised_task(_crash(ValueError(text)), name="boom")
    await asyncio.sleep(0.05)

    (record,) = captured.errors("background task 'boom'")
    assert "in _crash" in captured.traceback_text(record)


@pytest.mark.asyncio
async def test_logging_a_crashed_task_raises_nothing_into_the_event_loop(captured):
    _lifecycle().spawn_supervised_task(_crash(ValueError(BRACED)), name="boom")
    await asyncio.sleep(0.05)

    assert captured.loop_errors == []


@pytest.mark.asyncio
async def test_a_cancelled_supervised_task_is_not_logged_as_a_crash(captured):
    task = _lifecycle().spawn_supervised_task(asyncio.sleep(60), name="idle")
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.05)

    assert captured.errors("background task") == []


@pytest.mark.asyncio
async def test_a_finished_supervised_task_logs_nothing(captured):
    async def fine() -> str:
        return "ok"

    _lifecycle().spawn_supervised_task(fine(), name="fine")
    await asyncio.sleep(0.05)

    assert captured.errors("background task") == []


class _TickService:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def tick(self) -> None:
        raise self._exc


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["ordinary failure", BRACED], ids=["plain", "braces"])
async def test_a_failed_timer_firing_is_logged_with_message_and_traceback(captured, text):
    timer = Timer(interval=0.1)
    timer.method_name = "tick"
    timer.service_instance = _TickService(ValueError(text))

    await timer._execute_method()

    (record,) = captured.errors("timer method tick")
    assert text in record["message"]
    assert "in tick" in captured.traceback_text(record)

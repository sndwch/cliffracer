"""A dependency probe that returns something not awaitable fails with a message that names that.

A probe is called and its result awaited. One declared with a plain `def` that does its check and
returns, or an `async def` missing its `async`, used to fail with "object bool can't be used in
'await' expression", which says nothing about the probe, and on a default configuration the
payload said only "probe failed". The dependency is reported down either way (that is the safe
answer); what changes is that the text an operator reads says what to fix.

It cannot be refused when the dependency is declared, and the last test is why: a plain function
that RETURNS an awaitable is a working probe, and `lambda: client.ping()` is the shape most people
write.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from loguru import logger

from cliffracer.core.dependencies import Dependency, _run_one

pytestmark = pytest.mark.unit

EXPOSING = SimpleNamespace(expose_internal_errors=True)


def _sync_probe() -> bool:
    return True


async def _run(dep: Dependency, config=EXPOSING) -> tuple[dict, list[str]]:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        result = await _run_one(dep, config=config)
    finally:
        logger.remove(sink)
    return result, lines


async def test_a_probe_that_returns_a_bool_is_down_and_the_text_says_it_cannot_be_awaited():
    result, _ = await _run(Dependency(name="sync", probe=_sync_probe, timeout=1.0))

    assert result["ok"] is False
    assert "returned bool" in result["error"] and "async def" in result["error"], result["error"]
    assert "'await' expression" not in result["error"]


async def test_the_log_says_it_whether_or_not_the_payload_may():
    result, lines = await _run(Dependency(name="sync", probe=_sync_probe, timeout=1.0), config=None)

    assert result["ok"] is False and result["error"] == "probe failed"
    assert any("returned bool" in line and "async def" in line for line in lines), lines


async def test_a_probe_that_returns_none_is_named_for_what_it_returned():
    def forgot_to_return() -> None:
        return None

    result, _ = await _run(Dependency(name="none", probe=forgot_to_return, timeout=1.0))

    assert "returned NoneType" in result["error"], result["error"]


async def test_CONTROL_an_async_probe_is_unaffected():
    async def fine() -> None:
        return None

    result, _ = await _run(Dependency(name="fine", probe=fine, timeout=1.0))

    assert result["ok"] is True and result["error"] is None


async def test_CONTROL_a_plain_function_that_returns_an_awaitable_is_a_working_probe():
    async def ping() -> bool:
        return True

    result, _ = await _run(Dependency(name="lambda", probe=lambda: ping(), timeout=1.0))

    assert result["ok"] is True and result["error"] is None


async def test_CONTROL_a_probe_that_raises_its_own_error_keeps_it():
    async def broken() -> None:
        raise RuntimeError("redis is down")

    result, _ = await _run(Dependency(name="broken", probe=broken, timeout=1.0))

    assert result["ok"] is False and result["error"] == "RuntimeError: redis is down"


async def test_CONTROL_a_probe_that_returns_a_task_is_a_working_probe():
    """Awaitable is wider than coroutine: a Future or Task is awaited the same way."""

    async def ping() -> bool:
        return True

    result, _ = await _run(
        Dependency(name="task", probe=lambda: asyncio.ensure_future(ping()), timeout=1.0)
    )

    assert result["ok"] is True and result["error"] is None


async def test_CONTROL_a_probe_that_returns_an_object_with_await_is_a_working_probe():
    async def ping() -> bool:
        return True

    class Pending:
        def __await__(self):
            return ping().__await__()

    result, _ = await _run(Dependency(name="awaitable", probe=lambda: Pending(), timeout=1.0))

    assert result["ok"] is True and result["error"] is None

"""`/live` has three ways to learn whether a service is alive, and each is exercised.

`HealthListener._handle` asks `service.liveness_check()`, else `service.is_live()`, else reads the
service's own `_running` flag; each of the first two may be sync or a coroutine. Every other test
hands it a real `CliffracerService`, which always has `liveness_check`, so the other branches ran
nowhere: deleting `is_live` handling, the coroutine awaiting, or the inline fallback broke no
test. These use small stand-in services, so each branch is the only one that can answer.
"""

import asyncio
import json

import pytest

from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit


async def _get_live(service) -> tuple[int, dict]:
    listener = HealthListener(service, "127.0.0.1", 0)
    await listener.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        writer.write(b"GET /live HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        raw = await reader.read()
        writer.close()
    finally:
        await listener.stop()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body)


class _AsyncIsLive:
    def __init__(self, status: str) -> None:
        self._status = status

    async def is_live(self) -> dict:
        return {"status": self._status, "via": "is_live"}


class _SyncIsLive:
    def __init__(self, status: str) -> None:
        self._status = status

    def is_live(self) -> dict:
        return {"status": self._status, "via": "is_live"}


class _AsyncLivenessCheck:
    async def liveness_check(self) -> dict:
        return {"status": "healthy", "via": "liveness_check"}


class _SyncLivenessCheckThatWinsOverIsLive:
    def liveness_check(self) -> dict:
        return {"status": "healthy", "via": "liveness_check"}

    def is_live(self) -> dict:
        return {"status": "stopped", "via": "is_live"}


class _Bare:
    """No liveness probe at all: the listener reads `_running` and the config's name itself."""

    def __init__(self, running: bool | None = None, name: str | None = None) -> None:
        if running is not None:
            self._running = running
        if name is not None:
            self.config = type("Config", (), {"name": name})()


async def test_an_async_is_live_is_awaited_and_its_status_decides_the_code():
    assert await _get_live(_AsyncIsLive("healthy")) == (
        200,
        {"status": "healthy", "via": "is_live"},
    )
    status, body = await _get_live(_AsyncIsLive("stopped"))
    assert (status, body["status"]) == (503, "stopped")


async def test_a_sync_is_live_is_used_without_being_awaited():
    status, body = await _get_live(_SyncIsLive("healthy"))

    assert (status, body["via"]) == (200, "is_live")


async def test_an_async_liveness_check_is_awaited():
    assert await _get_live(_AsyncLivenessCheck()) == (
        200,
        {"status": "healthy", "via": "liveness_check"},
    )


async def test_liveness_check_is_preferred_to_is_live():
    status, body = await _get_live(_SyncLivenessCheckThatWinsOverIsLive())

    assert (status, body["via"]) == (200, "liveness_check")


async def test_with_no_probe_the_inline_fallback_reads_the_running_flag_and_the_name():
    running = await _get_live(_Bare(running=True, name="duck"))
    stopped = await _get_live(_Bare(running=False, name="duck"))

    assert running == (200, {"service": "duck", "status": "healthy"})
    assert stopped == (503, {"service": "duck", "status": "stopped"})


async def test_an_object_that_says_nothing_is_not_reported_healthy():
    """No probe and no `_running`: "could not determine" is not "yes", so a liveness probe
    gets 503 and `unknown`, under the default name "service"."""
    assert await _get_live(_Bare()) == (503, {"service": "service", "status": "unknown"})


class _BoolIsLive:
    def __init__(self, alive: bool, name: str | None = None) -> None:
        self._alive = alive
        if name is not None:
            self.config = type("Config", (), {"name": name})()

    def is_live(self) -> bool:
        return self._alive


async def test_an_is_live_that_answers_a_boolean_is_read_as_one():
    """`is_live() -> bool` is what the name reads as; it was answered with a 500."""
    assert await _get_live(_BoolIsLive(True, "duck")) == (
        200,
        {"service": "duck", "status": "healthy"},
    )
    assert await _get_live(_BoolIsLive(False, "duck")) == (
        503,
        {"service": "duck", "status": "stopped"},
    )

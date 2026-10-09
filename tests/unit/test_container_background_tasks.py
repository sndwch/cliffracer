"""Background task exception handling on client disconnection."""

import asyncio

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import refuse_a_reply_with_no_subject

pytestmark = pytest.mark.unit


class _MockEventMsg:
    def __init__(self, subject: str):
        self.subject = subject
        self.headers = None
        self.data = b"{}"
        self.reply = "mock.reply"
        self.respond_attempts = 0

    async def respond(self, data):
        # Counted before it refuses or raises: a handler that never tried to reply also finishes
        # with no exception, so "no exception" alone cannot say the disconnect was met.
        self.respond_attempts += 1
        refuse_a_reply_with_no_subject(self)
        raise ConnectionResetError("Client disconnected")


@pytest.mark.asyncio
async def test_unbounded_rpc_does_not_leak_exception_on_client_disconnect():
    class TestService(CliffracerService):
        @rpc
        async def work(self) -> str:
            return "done"

    config = ServiceConfig(name="test_svc", max_rpc_concurrency=None)  # Unbounded
    svc = TestService(config)
    svc._discover_handlers()

    msg = _MockEventMsg("test_svc.rpc.work")

    # This creates the background task
    await svc.container._on_rpc_request(msg)

    # Wait for the background task to complete
    bg_tasks = list(svc.container._active_tasks)
    assert len(bg_tasks) == 1

    task = bg_tasks[0]
    await asyncio.gather(*bg_tasks, return_exceptions=True)

    # The task should complete normally without raising an unhandled exception
    assert task.exception() is None
    # ...having tried to reply, once: the disconnect was met, not avoided
    assert msg.respond_attempts == 1, msg.respond_attempts


@pytest.mark.asyncio
async def test_describe_request_does_not_leak_exception_on_client_disconnect():
    class TestService(CliffracerService):
        @rpc
        async def work(self) -> str:
            return "done"

    config = ServiceConfig(name="test_svc")
    svc = TestService(config)

    msg = _MockEventMsg("test_svc.describe")
    logged: list[str] = []
    sink = logger.add(lambda message: logged.append(str(message)), level="DEBUG")
    try:
        await svc.container._on_describe_request(msg)
        bg_tasks = list(svc.container._active_tasks)
        assert len(bg_tasks) == 1

        task = bg_tasks[0]
        await asyncio.gather(*bg_tasks, return_exceptions=True)
    finally:
        logger.remove(sink)
    assert task.exception() is None
    assert msg.respond_attempts == 1, msg.respond_attempts
    # The disconnect was absorbed by describe's own reply guard. An escape to the outer handler
    # would also leave the task clean, and says so with a different line.
    assert any("Failed to send describe reply" in line for line in logged), logged
    assert not any("Error answering describe" in line for line in logged), logged

"""Background task exception handling on client disconnection."""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc


class _MockEventMsg:
    def __init__(self, subject: str):
        self.subject = subject
        self.headers = None
        self.data = b"{}"
        self.reply = "mock.reply"

    async def respond(self, data):
        raise ConnectionResetError("Client disconnected")


@pytest.mark.unit
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_describe_request_does_not_leak_exception_on_client_disconnect():
    class TestService(CliffracerService):
        @rpc
        async def work(self) -> str:
            return "done"

    config = ServiceConfig(name="test_svc")
    svc = TestService(config)

    msg = _MockEventMsg("test_svc.describe")

    await svc.container._on_describe_request(msg)
    bg_tasks = list(svc.container._active_tasks)
    assert len(bg_tasks) == 1

    task = bg_tasks[0]
    await asyncio.gather(*bg_tasks, return_exceptions=True)
    assert task.exception() is None

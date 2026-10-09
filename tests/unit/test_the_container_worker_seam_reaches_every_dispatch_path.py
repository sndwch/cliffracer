"""`container._run_worker = fn` intercepts a message on every path that dispatches one.

The setter is the seam tests patch to prove a worker-level gate, refusal or metric. Each
dispatch collaborator keeps its own delegate to the extension pipeline, so a setter that
writes to some of them intercepts those paths and silently does nothing for the rest.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, async_rpc, listener, rpc
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit


class _Demo(CliffracerService):
    @rpc
    async def ping(self) -> str:
        return "pong"

    @async_rpc
    async def notify(self) -> None:
        return None

    @listener("things.happened", fanout=True)
    async def on_thing(self, subject: str) -> None:
        return None

    @listener("jobs.queued", durable="jobs-worker")
    async def on_job(self, subject: str) -> None:
        return None


def _service() -> tuple[_Demo, list[str]]:
    svc = _Demo(
        ServiceConfig(
            name="demo",
            jetstream_enabled=True,
            jetstream_streams=[
                StreamSpec(name="JOBS", subjects=["jobs.>"]),
                StreamSpec(name="DLQ", subjects=["dlq.*"]),
            ],
        )
    )
    svc._discover_handlers()
    svc.nc = AsyncMock()
    svc.js = AsyncMock()
    intercepted: list[str] = []

    async def worker(ctx, call):
        intercepted.append(ctx.kind)
        return await call()

    svc.container._run_worker = worker
    return svc, intercepted


def _rpc_message(subject: str) -> AsyncMock:
    msg = AsyncMock()
    msg.subject = subject
    msg.reply = "_INBOX.1"
    msg.data = b"{}"
    msg.headers = None
    return msg


@pytest.mark.asyncio
async def test_an_rpc_request_goes_through_the_patched_worker():
    svc, intercepted = _service()
    await svc.container._handle_rpc_request(_rpc_message("demo.rpc.ping"))
    assert intercepted == ["rpc"]


@pytest.mark.asyncio
async def test_a_fire_and_forget_rpc_goes_through_the_patched_worker():
    svc, intercepted = _service()
    await svc.container._handle_async_request(_rpc_message("demo.async.notify"))
    assert intercepted == ["async_rpc"]


@pytest.mark.asyncio
async def test_a_describe_request_goes_through_the_patched_worker():
    svc, intercepted = _service()
    await svc.container._handle_describe_request(_rpc_message("demo.describe"))
    assert intercepted == ["describe"]


@pytest.mark.asyncio
async def test_a_core_event_goes_through_the_patched_worker():
    svc, intercepted = _service()
    msg = MockMessage(subject="things.happened", data=b"{}", headers={})
    await svc.container._dispatch_event(msg, pattern="things.happened", raise_on_error=True)
    assert intercepted == ["event"]


@pytest.mark.asyncio
async def test_a_jetstream_event_goes_through_the_patched_worker():
    svc, intercepted = _service()
    msg = AsyncMock()
    msg.subject = "jobs.queued"
    msg.data = json.dumps({}).encode()
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=1)

    await svc.container._handle_jetstream_event(msg, pattern="jobs.queued")

    assert intercepted == ["event"]
    msg.ack.assert_awaited_once()

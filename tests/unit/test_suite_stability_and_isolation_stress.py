"""Adversarial stress tests for test suite stability, mock cleanup, and task isolation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.connection import ConnectionManager
from cliffracer.core.container import Container
from cliffracer.core.correlation import correlation_id_var
from cliffracer.core.decorators import listener, rpc


class MockNatsMessage:
    """Mock NATS message simulating wire payload and response."""

    def __init__(self, data: bytes, reply: str = "_INBOX.test") -> None:
        self.data = data
        self.reply = reply
        self.subject = "dispatcher_stress.rpc.echo_fast"
        self.headers: dict[str, str] = {}
        self.responded_data: bytes | None = None

    async def respond(self, data: bytes) -> None:
        self.responded_data = data


class StressLifecycleService(CliffracerService):
    """Test service with registered RPC and listener handlers for lifecycle tests."""

    @rpc
    async def echo_fast(self, msg: str) -> str:
        return msg

    @listener("events.stress", fanout=True)
    async def on_event(self, count: int) -> None:
        pass


@pytest.mark.asyncio
async def test_repeated_service_lifecycle_task_drain_stress() -> None:
    """Stress-test repeated start/stop cycles verifying active task drainage and state reset."""
    num_cycles = 25

    def get_lifecycle_state(target: CliffracerService) -> tuple[bool, bool]:
        return target.container.lifecycle.is_running, target.container.lifecycle.is_stopped

    with (
        patch.object(ConnectionManager, "connect", new_callable=AsyncMock),
        patch.object(ConnectionManager, "disconnect", new_callable=AsyncMock),
    ):
        for cycle in range(num_cycles):
            config = ServiceConfig(name=f"lifecycle_svc_{cycle}", nats_url="nats://127.0.0.1:4222")
            svc = StressLifecycleService(config)

            mock_nc = AsyncMock()
            mock_nc.is_connected = True
            mock_nc.is_closed = False
            mock_nc.is_draining = False
            mock_nc.is_connecting = False
            mock_nc.drain = AsyncMock()
            mock_nc.close = AsyncMock()
            svc.container.connection.nc = mock_nc

            await svc.start()
            running, stopped = get_lifecycle_state(svc)
            assert running and not stopped

            # Spawn multiple background tasks through supervised spawner
            async def background_worker(work_id: int) -> int:
                await asyncio.sleep(0.01)
                return work_id

            tasks = [
                svc.container._spawn_supervised_task(background_worker(i), name=f"task_{i}")
                for i in range(10)
            ]
            assert len(svc.container.lifecycle.active_tasks) == 10

            # Stop service - must drain all active supervised tasks
            await svc.stop()

            running, stopped = get_lifecycle_state(svc)
            assert not running and stopped
            assert len(svc.container.lifecycle.active_tasks) == 0
            for t in tasks:
                assert t.done() is True


@pytest.mark.asyncio
async def test_mock_cleanup_and_class_restoration_across_tests() -> None:
    """Verify mocks applied during testing do not pollute subsequent service instances."""
    # 1. Apply a patch to Container._safe_ack
    with patch.object(Container, "_safe_ack", new_callable=AsyncMock) as mocked_ack:
        config = ServiceConfig(name="mock_svc_1")
        svc1 = CliffracerService(config)
        await svc1.container._safe_ack(None)
        assert mocked_ack.await_count == 1

    # 2. In a clean context, verify Container._safe_ack is restored to real method
    config2 = ServiceConfig(name="mock_svc_2")
    svc2 = CliffracerService(config2)
    assert not isinstance(svc2.container._safe_ack, AsyncMock)
    assert not isinstance(Container._safe_ack, AsyncMock)


@pytest.mark.unit
def test_correlation_id_context_cleanliness() -> None:
    """Verify correlation_id_var context is None between tests."""
    current = correlation_id_var.get()
    assert current is None or current == ""


@pytest.mark.asyncio
async def test_supervised_task_exception_retrieval_stress() -> None:
    """Verify exceptions inside supervised tasks are retrieved and do not leak."""
    config = ServiceConfig(name="exc_svc")
    svc = CliffracerService(config)

    async def failing_worker() -> None:
        await asyncio.sleep(0.01)
        raise RuntimeError("Simulated background worker crash")

    task = svc.container._spawn_supervised_task(failing_worker(), name="failing_task")
    assert task in svc.container.lifecycle.active_tasks

    # Wait for completion
    await asyncio.sleep(0.05)

    assert task.done() is True
    # The done callback discards it from active_tasks and retrieves exception
    assert task not in svc.container.lifecycle.active_tasks
    assert isinstance(task.exception(), RuntimeError)


@pytest.mark.asyncio
async def test_high_volume_concurrent_rpc_dispatch_drain() -> None:
    """Stress-test MessageDispatcher processing 200 concurrent RPC calls without task leaks."""
    config = ServiceConfig(name="dispatcher_stress", max_rpc_concurrency=50)
    svc = StressLifecycleService(config)
    svc._discover_handlers()

    dispatcher = svc.container.dispatcher

    async def run_rpc(idx: int) -> None:
        msg = MockNatsMessage(data=f'{{"msg": "call_{idx}"}}'.encode())
        await dispatcher.handle_rpc_request(msg)
        assert msg.responded_data is not None
        assert b"call_" in msg.responded_data

    tasks = [asyncio.create_task(run_rpc(i)) for i in range(200)]
    await asyncio.gather(*tasks)

    # Verify semaphore is fully released
    sem = dispatcher._get_rpc_semaphore()
    assert sem is not None
    assert sem._value == 50


@pytest.mark.asyncio
async def test_live_nats_connection_and_subscription_leak_free() -> None:
    """Verify on live NATS that start/stop does not leak lingering connections or subscriptions."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen("http://localhost:8222/connz", timeout=1.0) as resp:
            initial_connz = json.loads(resp.read().decode())
    except Exception:
        pytest.skip("NATS monitoring endpoint on port 8222 not reachable")

    config = ServiceConfig(name="live_leak_check", nats_url="nats://localhost:4222")
    svc = StressLifecycleService(config)

    await svc.start()
    assert bool(getattr(svc.container.lifecycle, "_running"))  # noqa: B009

    # Verify connection exists
    with urllib.request.urlopen("http://localhost:8222/connz", timeout=1.0) as resp:
        mid_connz = json.loads(resp.read().decode())
    assert mid_connz["num_connections"] >= initial_connz["num_connections"]

    # Stop service
    await svc.stop()
    assert not bool(getattr(svc.container.lifecycle, "_running"))  # noqa: B009

    # Verify connection was terminated
    await asyncio.sleep(0.1)
    with urllib.request.urlopen("http://localhost:8222/connz", timeout=1.0) as resp:
        post_connz = json.loads(resp.read().decode())

    # Find if any connection with name "live_leak_check" remains
    active_named = [
        c["name"] for c in post_connz.get("connections", []) if c.get("name") == "live_leak_check"
    ]
    assert active_named == []

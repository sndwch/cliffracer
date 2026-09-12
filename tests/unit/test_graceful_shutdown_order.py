"""Tests demonstrating graceful shutdown ordering.

Verifies that in-flight RPC tasks are completely drained before extension
resources are dismantled, preventing active handlers from encountering
prematurely closed extension resources (e.g. database pools, KV stores).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


class DatabaseExtension(Extension):
    """Simulates a stateful extension providing a database connection pool."""

    def __init__(self) -> None:
        self.connected: bool = False
        self.queries_executed: int = 0
        self.stopped_at: float | None = None

    async def setup(self, ctx: Any) -> None:
        self.connected = True

    async def start(self) -> None:
        self.connected = True

    async def stop(self) -> None:
        self.connected = False
        self.stopped_at = asyncio.get_running_loop().time()

    def query(self, sql: str) -> str:
        if not self.connected:
            raise RuntimeError(
                "Database pool is already closed! Teardown destroyed extension prematurely."
            )
        self.queries_executed += 1
        return f"result_for:{sql}"


class _MockMsg:
    """Mock NATS message for testing RPC dispatch and response."""

    def __init__(self, subject: str, data: dict[str, Any]) -> None:
        self.subject = subject
        self.data = json.dumps(data).encode()
        self.headers: dict[str, str] = {}
        self.response: dict[str, Any] | None = None

    async def respond(self, payload: bytes) -> None:
        self.response = json.loads(payload.decode())


@pytest.mark.asyncio
async def test_active_rpc_finishes_before_extensions_are_stopped() -> None:
    """Verify active in-flight RPC finishes before extensions are stopped."""
    timeline: list[str] = []

    class OrderService(CliffracerService):
        db = DatabaseExtension()

        @rpc
        async def query_order(self, order_id: str) -> str:
            timeline.append("rpc_started")
            # Simulate in-flight I/O before touching extension resource
            await asyncio.sleep(0.05)
            res = self.db.query(f"SELECT * FROM orders WHERE id='{order_id}'")
            timeline.append("rpc_queried_extension")
            return res

        async def on_shutdown(self) -> None:
            timeline.append("on_shutdown")

    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_nc.is_connected = True
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False

    with patch("nats.connect", return_value=mock_nc):
        svc = OrderService(ServiceConfig(name="order_service", health_port=0))
        await svc.start()

        assert svc.db.connected is True
        assert svc.db.queries_executed == 0

        # Simulate concurrent in-flight RPC task tracked in _active_tasks
        rpc_task = asyncio.create_task(svc.query_order("ord-123"))
        svc.container._active_tasks = {rpc_task}

        # Concurrently initiate service shutdown while RPC is in-flight
        stop_task = asyncio.create_task(svc.stop())

        # Await both to finish
        await asyncio.gather(rpc_task, stop_task)

        # The RPC must have completed without raising RuntimeError
        assert rpc_task.done()
        assert rpc_task.exception() is None
        assert rpc_task.result() == "result_for:SELECT * FROM orders WHERE id='ord-123'"

        # Extension query succeeded before extension was dismantled
        assert svc.db.queries_executed == 1
        assert svc.db.connected is False
        assert svc._stopped is True

        # Verify exact sequence of events
        assert timeline.index("rpc_started") < timeline.index("rpc_queried_extension")
        assert timeline.index("rpc_queried_extension") < timeline.index("on_shutdown")


@pytest.mark.asyncio
async def test_handle_rpc_request_drains_before_extension_teardown() -> None:
    """Verify _handle_rpc_request in _active_tasks finishes before extensions stop."""
    order_of_events: list[str] = []

    class TrackedExtension(Extension):
        def __init__(self) -> None:
            self.active = True

        async def stop(self) -> None:
            order_of_events.append("extension_stopped")
            self.active = False

    class PaymentService(CliffracerService):
        ext = TrackedExtension()

        @rpc
        async def process_payment(self, amount: int) -> dict[str, str]:
            order_of_events.append("handler_started")
            await asyncio.sleep(0.04)
            if not self.ext.active:
                raise RuntimeError("Extension was stopped while handler was processing!")
            order_of_events.append("handler_finished")
            return {"status": "paid", "amount": str(amount)}

        async def on_shutdown(self) -> None:
            order_of_events.append("on_shutdown")

    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_nc.is_connected = True
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False

    with patch("nats.connect", return_value=mock_nc):
        svc = PaymentService(ServiceConfig(name="payment_svc", health_port=0))
        await svc.start()

        msg = _MockMsg(
            subject="payment_svc.rpc.process_payment",
            data={"amount": 100},
        )

        handler_task = asyncio.create_task(svc.container._handle_rpc_request(msg))
        svc.container._active_tasks = {handler_task}

        await svc.stop()

        assert handler_task.done()
        assert handler_task.exception() is None
        assert msg.response is not None
        assert msg.response.get("success") is True
        assert msg.response.get("result") == {"status": "paid", "amount": "100"}

        # Handler must finish before extension stops and on_shutdown is called
        assert order_of_events == [
            "handler_started",
            "handler_finished",
            "on_shutdown",
            "extension_stopped",
        ]


@pytest.mark.asyncio
async def test_shutdown_sequence_comprehensive_order() -> None:
    """Verify shutdown sequence order: timers, listener, subs, tasks, hooks, extensions, NATS."""
    events: list[str] = []

    class LoggingExtension(Extension):
        async def stop(self) -> None:
            events.append("extensions_stopped")

    class FullLifecycleService(CliffracerService):
        ext = LoggingExtension()

        async def _stop_timers(self) -> None:
            events.append("timers_stopped")
            await self.container._stop_timers()

        async def on_shutdown(self) -> None:
            events.append("on_shutdown")
            await super().on_shutdown()

        async def disconnect(self) -> None:
            events.append("nats_disconnected")
            await super().disconnect()

    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_nc.is_connected = True
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False

    with patch("nats.connect", return_value=mock_nc):
        svc = FullLifecycleService(ServiceConfig(name="full_svc", health_port=0))
        await svc.start()

        # Monkey-patch health_listener.stop to log
        orig_health_stop = svc.health_listener.stop

        async def logged_health_stop() -> None:
            events.append("health_listener_stopped")
            await orig_health_stop()

        svc.health_listener.stop = logged_health_stop  # type: ignore[assignment]

        # Register an active task that finishes during drain
        async def background_worker() -> None:
            events.append("task_started")
            await asyncio.sleep(0.03)
            events.append("task_completed")

        t = asyncio.create_task(background_worker())
        svc.container._active_tasks = {t}

        await svc.stop()

        expected = [
            "timers_stopped",
            "health_listener_stopped",
            "task_started",
            "task_completed",
            "on_shutdown",
            "extensions_stopped",
            "nats_disconnected",
        ]
        assert events == expected


@pytest.mark.asyncio
async def test_shutdown_without_active_tasks_is_clean() -> None:
    """When no active tasks are present, shutdown executes cleanly."""
    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_nc.is_connected = True
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False

    with patch("nats.connect", return_value=mock_nc):
        svc = CliffracerService(ServiceConfig(name="clean_svc", health_port=0))
        await svc.start()
        # _active_tasks attribute absent or empty
        await svc.stop()
        assert svc._stopped is True
        assert svc._running is False


@pytest.mark.asyncio
async def test_active_task_exception_during_drain_does_not_abort_teardown() -> None:
    """If an active in-flight task raises an exception during drain,

    shutdown proceeds to stop extensions and disconnect cleanly.
    """

    class SafeExtension(Extension):
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class FailingTaskService(CliffracerService):
        ext = SafeExtension()

    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_nc.is_connected = True
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False

    with patch("nats.connect", return_value=mock_nc):
        svc = FailingTaskService(ServiceConfig(name="failing_task_svc", health_port=0))
        await svc.start()

        async def failing_worker() -> None:
            await asyncio.sleep(0.01)
            raise ValueError("In-flight task failed")

        task = asyncio.create_task(failing_worker())
        svc.container._active_tasks = {task}

        await svc.stop()

        assert svc.ext.stopped is True
        assert svc._stopped is True
        assert mock_nc.close.called

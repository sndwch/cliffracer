"""Tests verifying that in-flight event and async tasks are drained during graceful shutdown."""

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer.core.decorators import async_rpc, listener
from cliffracer.core.extension import Extension
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.testing.messages import MockMessage


class LifecycleTrackingExtension(Extension):
    """Extension that tracks when its lifecycle begins and ends."""

    def __init__(self) -> None:
        self.active = False
        self.stopped = False

    async def setup(self, ctx: Any) -> None:
        self.active = True

    async def stop(self) -> None:
        self.active = False
        self.stopped = True


@pytest.mark.unit
async def test_in_flight_event_drained_on_shutdown():
    """An event handler executing when stop() is called completes before extensions stop."""
    tracking_ext = LifecycleTrackingExtension()

    class EventDrainService(CliffracerService):
        def __init__(self, config: ServiceConfig) -> None:
            super().__init__(config)
            self.event_started = asyncio.Event()
            self.event_completed = False
            self.ext_active_during_handler = False

        @listener("drain.event", fanout=True)
        async def on_event(self, test: str = "") -> None:
            self.event_started.set()
            await asyncio.sleep(0.05)
            self.ext_active_during_handler = tracking_ext.active
            self.event_completed = True

    cfg = ServiceConfig(name="event_drain_svc", health_port=0)
    svc = EventDrainService(cfg)
    svc.container._extensions.append(tracking_ext)
    await tracking_ext.setup(svc)

    svc.container.nc = AsyncMock()
    svc.container.nc.is_connected = True
    svc.container.nc.is_closed = False
    svc.container.nc.is_draining = False

    with (
        patch.object(svc, "connect", new_callable=AsyncMock),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
    ):
        await svc.start()

        # Generate event callback and dispatch message
        cb = svc.container._make_event_callback("drain.event")
        msg = MockMessage(
            subject="drain.event",
            data=json.dumps({"test": "data"}).encode(),
            headers={"Content-Type": "application/json"},
        )

        # Calling cb spawns a task and adds it to _active_tasks
        await cb(msg)
        await svc.event_started.wait()

        assert len(svc.container._active_tasks) == 1

        # Now gracefully stop
        await svc.stop()

        # Event handler must have completed
        assert svc.event_completed is True
        # And during handler execution, extension must have still been active
        assert svc.ext_active_during_handler is True
        # Extension is now stopped
        assert tracking_ext.stopped is True
        assert len(svc.container._active_tasks) == 0


@pytest.mark.unit
async def test_in_flight_async_rpc_drained_on_shutdown():
    """An async RPC handler executing when stop() is called completes before extensions stop."""
    tracking_ext = LifecycleTrackingExtension()

    class AsyncRpcDrainService(CliffracerService):
        def __init__(self, config: ServiceConfig) -> None:
            super().__init__(config)
            self.rpc_started = asyncio.Event()
            self.rpc_completed = False
            self.ext_active_during_handler = False

        @async_rpc
        async def process_item(self, item_id: str) -> None:
            self.rpc_started.set()
            await asyncio.sleep(0.05)
            self.ext_active_during_handler = tracking_ext.active
            self.rpc_completed = True

    cfg = ServiceConfig(name="async_drain_svc", health_port=0)
    svc = AsyncRpcDrainService(cfg)
    svc.container._extensions.append(tracking_ext)
    await tracking_ext.setup(svc)

    svc.container.nc = AsyncMock()
    svc.container.nc.is_connected = True
    svc.container.nc.is_closed = False
    svc.container.nc.is_draining = False

    with (
        patch.object(svc, "connect", new_callable=AsyncMock),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
    ):
        await svc.start()

        msg = MockMessage(
            subject="async_drain_svc.async.process_item",
            data=json.dumps({"item_id": "42"}).encode(),
            headers={"Content-Type": "application/json"},
        )

        await svc.container._on_async_request(msg)
        await svc.rpc_started.wait()

        assert len(svc.container._active_tasks) == 1

        await svc.stop()

        assert svc.rpc_completed is True
        assert svc.ext_active_during_handler is True
        assert tracking_ext.stopped is True
        assert len(svc.container._active_tasks) == 0


@pytest.mark.unit
async def test_shutdown_timeout_cancels_stuck_tasks():
    """Tasks exceeding shutdown_timeout are cancelled so shutdown does not hang."""

    class HangingService(CliffracerService):
        def __init__(self, config: ServiceConfig) -> None:
            super().__init__(config)
            self.task_started = asyncio.Event()
            self.cancelled = False

        @listener("hang.event", fanout=True)
        async def on_hang(self, subject: str) -> None:
            self.task_started.set()
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    cfg = ServiceConfig(name="hanging_svc", health_port=0, shutdown_timeout=0.05)
    svc = HangingService(cfg)

    svc.container.nc = AsyncMock()
    svc.container.nc.is_connected = True
    svc.container.nc.is_closed = False
    svc.container.nc.is_draining = False

    with (
        patch.object(svc, "connect", new_callable=AsyncMock),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
    ):
        await svc.start()

        cb = svc.container._make_event_callback("hang.event")
        msg = MockMessage(subject="hang.event", data=b"{}")
        await cb(msg)
        await svc.task_started.wait()

        assert len(svc.container._active_tasks) == 1

        start_time = time.monotonic()
        await svc.stop()
        duration = time.monotonic() - start_time

        # Stop must complete quickly without hanging for 30s
        assert duration < 0.5
        assert svc.cancelled is True
        assert svc._stopped is True


@pytest.mark.unit
async def test_pull_consumer_in_flight_event_shielded_and_drained():
    """In-flight pull consumer message is shielded from loop cancellation and drained."""
    tracking_ext = LifecycleTrackingExtension()

    class PullConsumerDrainService(CliffracerService):
        def __init__(self, config: ServiceConfig) -> None:
            super().__init__(config)
            self.handler_started = asyncio.Event()
            self.handler_completed = False

        @listener("pull.drain", durable="pull_drain_durable")
        async def on_pull_event(self, key: str = "") -> None:
            self.handler_started.set()
            await asyncio.sleep(0.05)
            self.handler_completed = True

    cfg = ServiceConfig(
        name="pull_drain_svc",
        health_port=0,
        jetstream_enabled=True,
    )
    svc = PullConsumerDrainService(cfg)
    svc._discover_handlers()
    svc.container._extensions.append(tracking_ext)
    await tracking_ext.setup(svc)

    mock_sub = AsyncMock()
    mock_msg = MockMessage(
        subject="pull.drain",
        data=json.dumps({"key": "val"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    mock_sub.fetch = AsyncMock(return_value=[mock_msg])

    # Run _pull_once
    pull_task = asyncio.create_task(svc.container._pull_once(mock_sub, pattern="pull.drain"))

    await svc.handler_started.wait()
    assert len(svc.container._active_tasks) == 1

    # Simulate loop cancellation
    pull_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pull_task

    # The in-flight task must still be running in _active_tasks due to asyncio.shield
    assert len(svc.container._active_tasks) == 1

    # Stop drains _active_tasks
    await svc.stop()

    assert svc.handler_completed is True
    assert len(svc.container._active_tasks) == 0

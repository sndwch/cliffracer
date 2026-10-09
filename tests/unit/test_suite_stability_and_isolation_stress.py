"""Adversarial stress tests for test suite stability, mock cleanup, and task isolation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.connection import ConnectionManager
from cliffracer.core.container import Container
from cliffracer.core.correlation import CorrelationContext, correlation_id_var
from cliffracer.core.decorators import listener, rpc
from cliffracer.core.extension import WorkerContext
from cliffracer.testing import refuse_a_reply_with_no_subject
from tests.conftest import broker_url

pytestmark = pytest.mark.unit


class MockNatsMessage:
    """Mock NATS message simulating wire payload and response."""

    def __init__(self, data: bytes, reply: str = "_INBOX.test") -> None:
        self.data = data
        self.reply = reply
        self.subject = "dispatcher_stress.rpc.echo_fast"
        self.headers: dict[str, str] = {}
        self.responded_data: bytes | None = None

    async def respond(self, data: bytes) -> None:
        refuse_a_reply_with_no_subject(self)
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
            config = ServiceConfig(name=f"lifecycle_svc_{cycle}", nats_url=broker_url())
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
    assert callable(svc2.container._safe_ack)

    mock_msg = AsyncMock()
    mock_msg.ack = AsyncMock()
    result = await svc2.container._safe_ack(mock_msg)
    assert result is True
    assert mock_msg.ack.await_count == 1


def test_correlation_id_context_cleanliness() -> None:
    """Verify correlation_id_var context is strictly None between synchronous tests."""
    current = correlation_id_var.get()
    assert current is None, f"correlation_id_var leaked value: {current!r}"


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
async def test_shutdown_drains_a_task_spawned_by_a_task() -> None:
    """Shutdown waits for the whole tree, not one snapshot of it.

    A supervised task may spawn another before it finishes. Awaiting a single
    snapshot of `active_tasks` returns while the second is still running, and
    the rest of shutdown -- disconnect included -- proceeds underneath it.
    """
    svc = CliffracerService(ServiceConfig(name="drain_generations"))
    finished: list[str] = []

    async def child() -> None:
        await asyncio.sleep(0.05)
        finished.append("child")

    async def parent() -> None:
        await asyncio.sleep(0.01)
        svc.container._spawn_supervised_task(child(), name="child_task")
        finished.append("parent")

    svc.container._spawn_supervised_task(parent(), name="parent_task")

    await svc.container.lifecycle.drain_active_tasks(timeout=5.0)

    assert finished == ["parent", "child"], f"the spawned task must be drained too, saw {finished}"
    assert svc.container.lifecycle.active_tasks == set()


@pytest.mark.parametrize("yields_before_drain", [0, 1, 2, 3])
@pytest.mark.asyncio
async def test_the_drain_empties_the_set_whatever_the_callbacks_have_done(
    yields_before_drain: int,
) -> None:
    """A finished task is removed from `active_tasks` by a done-callback, which
    asyncio runs on a later loop iteration. So "every task is done" and "the set
    is empty" are different states, and there is a window between them.

    Draining on `done()` alone returns inside that window and leaves finished
    tasks listed as active. The parametrized yield count walks across the
    window rather than trying to land in it: at exactly one yield the tasks
    have finished and their callbacks have not run.
    """
    svc = CliffracerService(ServiceConfig(name=f"drain_window_{yields_before_drain}"))

    async def quick() -> int:
        return 1

    tasks = [svc.container._spawn_supervised_task(quick(), name=f"t{i}") for i in range(3)]
    for _ in range(yields_before_drain):
        await asyncio.sleep(0)

    await svc.container.lifecycle.drain_active_tasks(timeout=5.0)

    assert svc.container.lifecycle.active_tasks == set(), (
        f"drain returned with {len(svc.container.lifecycle.active_tasks)} task(s) "
        f"still listed after {yields_before_drain} yield(s)"
    )
    assert all(t.done() for t in tasks)


@pytest.mark.asyncio
async def test_high_volume_concurrent_rpc_dispatch_drain() -> None:
    """Stress-test MessageDispatcher processing 200 concurrent RPC calls without task leaks."""
    config = ServiceConfig(name="dispatcher_stress", max_rpc_concurrency=50)
    svc = StressLifecycleService(config)
    svc._discover_handlers()

    dispatcher = svc.container.dispatcher

    messages = [MockNatsMessage(data=f'{{"msg": "call_{i}"}}'.encode()) for i in range(200)]

    await asyncio.gather(*(dispatcher.on_rpc_request(m) for m in messages))

    # Drain until nothing is left rather than once: a task that spawns another
    # puts it in the set after a single snapshot was taken, so the assertions
    # below would run against a set that is still filling. The bound keeps a
    # task that respawns forever from hanging the suite instead of failing it.
    for _ in range(10):
        pending = list(svc.container.lifecycle.active_tasks)
        if not pending:
            break
        await asyncio.gather(*pending)
    else:
        raise AssertionError(
            f"still spawning after 10 drains: "
            f"{len(svc.container.lifecycle.active_tasks)} task(s) outstanding"
        )

    for m in messages:
        assert m.responded_data is not None
        assert b"call_" in m.responded_data

    # Verify semaphore is fully released
    sem = dispatcher._get_rpc_semaphore()
    assert sem is not None
    assert sem._value == 50
    assert len(svc.container.lifecycle.active_tasks) == 0


@pytest.mark.asyncio
async def test_async_dispatch_correlation_isolation_across_messages():
    """Verify successive dispatches on one service instance do not leak correlation IDs."""
    config = ServiceConfig(name="corr_isolation_svc")
    svc = CliffracerService(config)
    await svc.container._setup_extensions()

    observed_ids: list[str | None] = []

    async def handler():
        observed_ids.append(CorrelationContext.get())
        return "ok"

    # Dispatch 1: explicit correlation ID
    ctx1 = WorkerContext(
        kind="rpc",
        subject="corr.test.1",
        headers={"correlation_id": "caller_specified_id_101"},
        correlation_id=None,
        payload={},
    )
    await svc.container._run_worker(ctx1, handler)
    assert observed_ids[-1] == "caller_specified_id_101"

    # Dispatch 2: message without correlation ID; must not inherit from message 1
    ctx2 = WorkerContext(
        kind="rpc",
        subject="corr.test.2",
        headers={},
        correlation_id=None,
        payload={},
    )
    await svc.container._run_worker(ctx2, handler)
    assert observed_ids[-1] is not None
    assert observed_ids[-1] != "caller_specified_id_101", (
        "Dispatch 2 inherited correlation ID from dispatch 1"
    )

    # Dispatch 3: handler raises exception; verify teardown clears token
    async def failing_handler():
        observed_ids.append(CorrelationContext.get())
        raise RuntimeError("Simulated handler crash")

    ctx3 = WorkerContext(
        kind="rpc",
        subject="corr.test.3",
        headers={"correlation_id": "failing_msg_id"},
        correlation_id=None,
        payload={},
    )
    with pytest.raises(RuntimeError):
        await svc.container._run_worker(ctx3, failing_handler)

    # Dispatch 4: verify clean slate following exception
    ctx4 = WorkerContext(
        kind="rpc",
        subject="corr.test.4",
        headers={},
        correlation_id=None,
        payload={},
    )
    await svc.container._run_worker(ctx4, handler)
    assert observed_ids[-1] != "failing_msg_id"

    # In caller task, ambient context must remain None
    assert CorrelationContext.get() is None

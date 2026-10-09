"""The supervisor sees process exit even when an order ignores cancellation."""

import asyncio
import contextvars
import subprocess
import sys

import pytest

from cliffracer.core.loop_host import run

pytestmark = pytest.mark.unit

PROCESS = """
import asyncio, os, signal, sys
from unittest.mock import AsyncMock, patch
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.loop_host import run, abandon
from cliffracer.runners.orchestrator import ServiceRunner, ServiceOrchestrator

entry = sys.argv[1]
async def fulfill():
    print('ORDER STARTED', flush=True)
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            print('ORDER REFUSED CANCELLATION', flush=True)

class Orders(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name='orders', health_listener=False,
                                     shutdown_timeout=0.05, auto_restart=entry == 'restart'))

    async def on_startup(self):
        self.container.lifecycle.spawn_supervised_task(fulfill(), name='fulfill-order')
        def stop():
            if entry == 'restart':
                broker.is_closed = True
            elif entry == 'service':
                self._running = False
            else:
                os.kill(os.getpid(), signal.SIGTERM)
        asyncio.get_running_loop().call_later(0.05, stop)

async def main():
    task = asyncio.create_task(fulfill(), name='fulfill-order')
    await asyncio.sleep(0)
    if entry == 'abandoned':
        abandon(task)
    print('MAIN RETURNED', flush=True)
    return 7

broker = AsyncMock()
broker.is_closed = False
broker.is_connected = True
broker.is_draining = False
with patch('cliffracer.core.dial.connect', AsyncMock(return_value=broker)):
    if entry == 'service':
        Orders().run()
    elif entry in ('runner', 'restart'):
        ServiceRunner(Orders).run_forever()
    elif entry == 'orchestrator':
        services = ServiceOrchestrator()
        services.add_service(Orders)
        services.run_forever()
    elif entry == 'stdlib':
        asyncio.run(main())
    else:
        assert run(main(), teardown_timeout=60 if entry == 'abandoned' else 0.05) == 7
print('PROCESS EXITING', flush=True)
"""


@pytest.mark.parametrize("entry", ["service", "runner", "orchestrator", "host", "abandoned"])
def test_order_process_exits_with_unfinished_work_named(entry: str) -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-c", PROCESS, entry], capture_output=True, text=True, timeout=5
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"{entry} kept the order process alive after shutdown: {error.stdout!r}")
    assert result.returncode == 0, result.stderr
    assert "ORDER STARTED" in result.stdout
    assert "ORDER REFUSED CANCELLATION" in result.stdout
    assert "PROCESS EXITING" in result.stdout
    assert "fulfill-order" in result.stderr and "did not stop" in result.stderr


def test_CONTROL_an_unbounded_host_cannot_exit_after_the_main_coroutine_returns() -> None:
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        subprocess.run(
            [sys.executable, "-c", PROCESS, "stdlib"], capture_output=True, text=True, timeout=2
        )
    output = caught.value.stdout or b""
    assert b"MAIN RETURNED" in output
    assert b"ORDER REFUSED CANCELLATION" in output
    assert b"PROCESS EXITING" not in output


def test_host_preserves_results_context_and_cooperative_cleanup() -> None:
    tenant = contextvars.ContextVar("tenant", default="unset")
    tenant.set("warehouse")
    completed = []

    async def reconcile() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            completed.append("receipt saved")

    async def orders() -> str:
        asyncio.create_task(reconcile())
        await asyncio.sleep(0)
        return tenant.get()

    assert run(orders(), teardown_timeout=1) == "warehouse"
    assert completed == ["receipt saved"]


def test_host_propagates_application_failure() -> None:
    async def orders() -> None:
        raise ValueError("unknown warehouse")

    with pytest.raises(ValueError, match="unknown warehouse"):
        run(orders())


async def test_host_rejects_a_nested_loop_without_replacing_the_callers_loop() -> None:
    loop = asyncio.get_running_loop()
    coroutine = asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match="running event loop"):
            run(coroutine)
        assert asyncio.get_running_loop() is loop
    finally:
        coroutine.close()


def test_runner_exits_nonzero_instead_of_replacing_unfinished_order_processing() -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-c", PROCESS, "restart"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"runner replaced unfinished order processing: {error.stdout!r}")
    assert result.returncode == 1, result.stderr
    assert result.stdout.count("ORDER STARTED") == 1
    assert "Cannot restart service with unfinished shutdown tasks" in result.stderr
    assert "Traceback" not in result.stderr

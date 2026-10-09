"""The logging decorators wrap a real service handler without changing its shape.

Handler discovery reads the live signature of every decorated method, so a
wrapper that hides it makes the service refuse to start. These tests declare the
decorators under ``@rpc`` and ``@listener`` on a service and run discovery.
"""

import asyncio
import inspect

import pytest
from cliffracer_logging.config import (
    ContextualLogger,
    log_event_handling,
    log_rpc_calls,
)
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, listener, rpc

pytestmark = pytest.mark.unit

LOGGER = ContextualLogger("decorated_service")


class LoggedRpcService(CliffracerService):
    @rpc
    @log_rpc_calls(LOGGER)
    async def place(self, item: str, qty: int) -> str:
        """Place an order."""
        return f"{qty} x {item}"


class LoggedEventService(CliffracerService):
    @listener("orders.created", fanout=True)
    @log_event_handling(LOGGER)
    async def on_created(self, subject: str, order_id: str = "") -> str:
        return f"{subject}:{order_id}"


class FailingRpcService(CliffracerService):
    @rpc
    @log_rpc_calls(LOGGER)
    async def explode(self, item: str) -> str:
        raise ValueError(f"cannot place {item}")


class FailingEventService(CliffracerService):
    @listener("orders.failed", fanout=True)
    @log_event_handling(LOGGER)
    async def on_failed(self, subject: str, order_id: str = "") -> None:
        raise ValueError(f"cannot handle {order_id}")


@pytest.fixture
def captured_lines():
    lines: list[str] = []
    handler_id = logger.add(lambda message: lines.append(message.record["message"]), level="DEBUG")
    yield lines
    logger.remove(handler_id)


def _discovered(service_cls: type[CliffracerService]) -> CliffracerService:
    service = service_cls(ServiceConfig(name="decorated_service"))
    service.container.discover_handlers()
    return service


def test_a_service_with_a_logged_rpc_handler_discovers_it():
    registry = _discovered(LoggedRpcService).container.registry

    assert list(registry.rpc_handlers) == ["place"]


def test_a_service_with_a_logged_event_handler_discovers_it():
    registry = _discovered(LoggedEventService).container.registry

    assert list(registry.event_handler_names.values()) == ["on_created"]


@pytest.mark.asyncio
async def test_a_logged_rpc_handler_runs_and_logs_both_lines(captured_lines):
    registry = _discovered(LoggedRpcService).container.registry

    assert await registry.rpc_handlers["place"](item="widget", qty=3) == "3 x widget"
    assert captured_lines == ["RPC call started: place", "RPC call completed: place"]


@pytest.mark.asyncio
async def test_a_logged_event_handler_runs_and_logs_both_lines(captured_lines):
    registry = _discovered(LoggedEventService).container.registry
    (handler,) = registry.event_handlers.values()

    assert await handler(subject="orders.created", order_id="o-1") == "orders.created:o-1"
    assert captured_lines == [
        "Event handling started: on_created",
        "Event handling completed: on_created",
    ]


def test_a_decorated_function_keeps_its_name_doc_and_signature():
    @log_rpc_calls(LOGGER)
    async def place(service, item: str, qty: int) -> str:
        """Place an order."""
        return item

    assert place.__name__ == "place"
    assert place.__doc__ == "Place an order."
    assert list(inspect.signature(place).parameters) == ["service", "item", "qty"]


def test_a_sync_function_stays_sync_under_either_decorator():
    @log_rpc_calls(LOGGER)
    def add(service, a, b):
        return a + b

    @log_event_handling(LOGGER)
    def note(service, subject):
        return subject

    assert not asyncio.iscoroutinefunction(add)
    assert not asyncio.iscoroutinefunction(note)
    assert add(None, 1, 2) == 3
    assert note(None, "s") == "s"


@pytest.mark.asyncio
async def test_a_failing_rpc_handler_raises_and_logs_the_failure(captured_lines):
    registry = _discovered(FailingRpcService).container.registry

    with pytest.raises(ValueError, match="cannot place widget"):
        await registry.rpc_handlers["explode"](item="widget")

    assert captured_lines == ["RPC call started: explode", "RPC call failed: explode"]


@pytest.mark.asyncio
async def test_a_failing_event_handler_raises_and_logs_the_failure(captured_lines):
    registry = _discovered(FailingEventService).container.registry
    (handler,) = registry.event_handlers.values()

    with pytest.raises(ValueError, match="cannot handle o-1"):
        await handler(subject="orders.failed", order_id="o-1")

    assert captured_lines == [
        "Event handling started: on_failed",
        "Event handling failed: on_failed",
    ]


def test_a_failing_sync_function_raises_and_logs_the_failure_under_either_decorator(
    captured_lines,
):
    @log_rpc_calls(LOGGER)
    def explode_rpc(service):
        raise ValueError("sync rpc failure")

    @log_event_handling(LOGGER)
    def explode_event(service, subject):
        raise ValueError("sync event failure")

    with pytest.raises(ValueError, match="sync rpc failure"):
        explode_rpc(None)
    with pytest.raises(ValueError, match="sync event failure"):
        explode_event(None, "s")

    assert captured_lines == [
        "RPC call started: explode_rpc",
        "RPC call failed: explode_rpc",
        "Event handling started: explode_event",
        "Event handling failed: explode_event",
    ]

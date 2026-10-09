"""Dispatcher cleanups.

- `MessageDispatcher.container` was assigned by the container and read by nobody: the vestige of
  an override mechanism that read `container.__dict__`. The dispatcher holds no reference back.
- The module's `__all__` listed two private names (`_HandlerMeta`, `_JetStreamHeartbeat`),
  advertising internals as public surface.
- `RpcDispatcher._bounded_handle_rpc` swallowed anything that escaped `handle_rpc_request`
  at DEBUG, so a reply that could not be sent, or a bug in dispatch, left no trace at the usual
  log level.
"""

from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core import dispatcher as dispatcher_module
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


def _service() -> CliffracerService:
    return CliffracerService(ServiceConfig(name="svc", health_port=0))


def test_the_dispatcher_holds_no_reference_back_to_the_container():
    dispatcher = _service().container.dispatcher

    assert not hasattr(dispatcher, "container")


def test_the_dispatcher_modules_public_surface_names_no_private_names():
    private = [name for name in dispatcher_module.__all__ if name.startswith("_")]

    assert private == []


def test_CONTROL_every_name_in_all_exists():
    assert all(hasattr(dispatcher_module, name) for name in dispatcher_module.__all__)


async def test_a_request_failure_that_escapes_the_rpc_handler_is_logged_above_debug():
    dispatcher = _service().container.dispatcher
    dispatcher.rpc.handle_rpc_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("the reply could not be sent")
    )
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="ERROR")
    try:
        await dispatcher.rpc._bounded_handle_rpc(
            MockMessage(subject="svc.rpc.anything", data=b"{}", headers={})
        )
    finally:
        logger.remove(sink)

    assert any("the reply could not be sent" in line for line in lines), lines

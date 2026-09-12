"""Tests for decorator handling on services with default configuration."""

import pytest

from cliffracer.core.decorators import rpc
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit


def _service():
    return CliffracerService(ServiceConfig(name="bare"))


def test_an_rpc_handler_is_registered_together_with_its_spec():
    """Verify bare service registers handler and builds validation spec."""

    class Svc(CliffracerService):
        @rpc
        async def do_thing(self, value: str) -> str:
            return value

    svc = Svc(ServiceConfig(name="bare"))
    svc._discover_handlers()

    assert svc.container.registry.rpc_handlers["do_thing"].__func__ is Svc.do_thing
    assert [p.name for p in svc.container.registry.rpc_specs["do_thing"].params] == ["value"]


def test_broadcast_falls_back_to_a_plain_event_handler():
    svc = _service()

    def handler():
        return None

    svc.register_broadcast_handler("things.*", handler)

    assert svc.container.registry.event_handlers["things.*"] is handler

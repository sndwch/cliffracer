"""`serialization_format="msgpack"` without the `msgpack` package is refused before the service connects.

The package is an optional extra. A service configured for it on a host without it constructed,
connected and served, and the first publish, call or reply that serialised raised `ImportError`
inside a handler or publisher, in production, long after startup.
"""

from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core import validation
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


class Svc(CliffracerService):
    started: list[str]

    async def on_startup(self) -> None:
        self.started.append("on_startup")


@pytest.fixture
def without_msgpack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation, "msgpack", None)


@pytest.mark.asyncio
async def test_start_refuses_before_it_connects_or_runs_anything(without_msgpack):
    service = Svc(ServiceConfig(name="orders", serialization_format="msgpack", health_port=0))
    service.started = []
    service.connect = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ConfigurationError) as caught:
        await service.start()

    message = str(caught.value)
    assert "'orders'" in message, message
    assert "serialization_format='msgpack'" in message, message
    assert "pip install 'cliffracer[msgpack]'" in message, message
    assert "serialization_format='json'" in message, message
    service.connect.assert_not_awaited()
    assert service.started == []


@pytest.mark.asyncio
async def test_a_json_service_starts_without_the_package(without_msgpack):
    service = Svc(ServiceConfig(name="orders", health_port=0))
    service.started = []
    service.connect = AsyncMock()  # type: ignore[method-assign]
    service.container._discover_for_startup()

    HandlerDiscovery.validate_serialization_available(service.config)


def test_a_msgpack_service_is_accepted_when_the_package_is_installed():
    pytest.importorskip("msgpack")
    config = ServiceConfig(name="orders", serialization_format="msgpack", health_port=0)

    HandlerDiscovery.validate_serialization_available(config)


def test_the_check_reads_the_package_at_call_time_not_import_time(monkeypatch):
    """A package installed (or hidden) after import is what the check sees."""
    config = ServiceConfig(name="orders", serialization_format="msgpack", health_port=0)
    monkeypatch.setattr(validation, "msgpack", object())
    HandlerDiscovery.validate_serialization_available(config)

    monkeypatch.setattr(validation, "msgpack", None)
    with pytest.raises(ConfigurationError):
        HandlerDiscovery.validate_serialization_available(config)

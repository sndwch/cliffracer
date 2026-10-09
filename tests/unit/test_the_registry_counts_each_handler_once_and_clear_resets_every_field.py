"""`/health`'s feature counts count a handler once, and `clear()` resets the whole registry.

Discovery registers a `@broadcast` handler in `event_handlers` as well as
`broadcast_handlers`, because it is subscribed like a listener. `feature_counts()`
counted it under both, so a service with one broadcast and no listeners reported
`events: 1, broadcasts: 1`: two handlers where there is one. The unit test that
covered it filled the registry by hand with disjoint entries, a state discovery
never produces, so these go through discovery.

`clear()` was thirteen hand-written `.clear()` calls to keep in step with the
fields, asserted for seven of them. It now walks the dataclass's fields, and the
test fills every field and checks every field.
"""

from dataclasses import fields

import pytest

from cliffracer import CliffracerService, ServiceConfig, broadcast, listener
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.registry import ServiceRegistry

pytestmark = pytest.mark.unit


class OneBroadcast(CliffracerService):
    @broadcast("alerts.fire")
    async def on_alert(self, subject: str, level: str = "") -> None:
        pass


class OneListenerOneBroadcast(CliffracerService):
    @listener("orders.created", fanout=True)
    async def on_order(self, subject: str) -> None:
        pass

    @broadcast("alerts.fire")
    async def on_alert(self, subject: str, level: str = "") -> None:
        pass


def _counts(service_class) -> dict[str, int]:
    config = ServiceConfig(name="counted")
    return HandlerDiscovery.discover(service_class(config), config).feature_counts()


def test_a_service_with_one_broadcast_reports_one_handler():
    assert _counts(OneBroadcast) == {"rpc": 0, "events": 0, "timers": 0, "broadcasts": 1}


def test_a_listener_and_a_broadcast_are_each_counted_once():
    assert _counts(OneListenerOneBroadcast) == {
        "rpc": 0,
        "events": 1,
        "timers": 0,
        "broadcasts": 1,
    }


def test_CONTROL_the_broadcast_is_registered_as_an_event_handler_too():
    """The premise of the fix: without it, `events` would already be right."""
    config = ServiceConfig(name="counted")
    registry = HandlerDiscovery.discover(OneBroadcast(config), config)

    assert set(registry.broadcast_handlers) <= set(registry.event_handlers)
    assert registry.broadcast_handlers


def _filled() -> ServiceRegistry:
    registry = ServiceRegistry()
    for registered in fields(registry):
        container = getattr(registry, registered.name)
        if isinstance(container, dict):
            container["k"] = object()
        elif isinstance(container, set):
            container.add("k")
        else:
            container.append(object())
    return registry


def test_clear_empties_every_field_of_the_registry():
    registry = _filled()
    assert all(len(getattr(registry, f.name)) == 1 for f in fields(registry))

    registry.clear()

    left = {
        f.name: len(getattr(registry, f.name))
        for f in fields(registry)
        if getattr(registry, f.name)
    }
    assert not left, left


def test_CONTROL_the_registry_has_the_fields_this_test_walks():
    names = {f.name for f in fields(ServiceRegistry)}

    assert len(names) >= 12
    assert {
        "event_specs_by_subject",
        "event_schemas",
        "dependencies",
        "event_handler_names",
    } <= names
    assert "entrypoints" not in names

"""A refused discovery puts the registry back.

Discovery fills the registry as it walks the handlers and judges the whole at the end, so a
refusal used to leave the refused handlers in it, and `describe`, `/info` and anything else that
reads the registry advertised a service that was never allowed to start.
"""

from dataclasses import fields

import pytest

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, listener
from cliffracer.core.registry import ServiceRegistry

pytestmark = pytest.mark.unit


def _config(**overrides):
    return ServiceConfig(name="svc", namespace="ns", jetstream_enabled=True, **overrides)


class _SharesADurable(CliffracerService):
    @listener("events.a", durable="shared")
    async def on_a(self, subject: str) -> None:
        pass

    @listener("events.b", durable="shared")
    async def on_b(self, subject: str) -> None:
        pass


def test_a_refused_discovery_leaves_no_handler_in_the_registry():
    svc = _SharesADurable(_config())
    registry = svc.container.registry

    with pytest.raises(ConfigurationError):
        svc._discover_handlers()

    assert registry.event_handlers == {}
    assert registry.event_specs_by_subject == {}
    assert registry.event_durables == {}
    assert registry.event_handler_names == {}


def test_a_refused_service_does_not_advertise_the_handlers_it_was_refused():
    svc = _SharesADurable(_config())

    with pytest.raises(ConfigurationError):
        svc._discover_handlers()

    assert svc.get_service_info()["event_patterns"] == []


def test_a_refused_discovery_keeps_what_the_registry_held_before_it():
    """Put back is put BACK, not emptied."""
    svc = _SharesADurable(_config())
    registry = svc.container.registry
    registry.rpc_handlers["kept"] = len
    registry.event_fanout.add("kept.fanout")
    registry.timers.append("kept-timer")

    with pytest.raises(ConfigurationError):
        svc._discover_handlers()

    assert registry.rpc_handlers == {"kept": len}
    assert registry.event_fanout == {"kept.fanout"}
    assert registry.timers == ["kept-timer"]


def test_a_refused_discovery_restores_the_collections_it_handed_out_not_new_ones():
    """Collaborators hold the registry's own dictionaries, so restoring must mutate them in place.

    `dependencies` is left out: dependency discovery replaces that list with a new one as part of
    its work, before any refusal, so it is not a collection restore could keep in place.
    """
    svc = _SharesADurable(_config())
    registry = svc.container.registry
    held = {
        registered.name: getattr(registry, registered.name)
        for registered in fields(registry)
        if registered.name != "dependencies"
    }

    with pytest.raises(ConfigurationError):
        svc._discover_handlers()

    assert held
    assert all(getattr(registry, name) is collection for name, collection in held.items())


def test_the_refusal_is_still_raised_on_every_later_call():
    svc = _SharesADurable(_config())

    with pytest.raises(ConfigurationError) as first:
        svc._discover_handlers()
    with pytest.raises(ConfigurationError) as second:
        svc._discover_handlers()

    assert second.value is first.value


def test_CONTROL_a_discovery_that_succeeds_keeps_what_it_found():
    class Fine(CliffracerService):
        @listener("events.a", durable="one")
        async def on_a(self, subject: str) -> None:
            pass

    svc = Fine(_config())
    svc._discover_handlers()

    assert svc.container.registry.event_durables == {"ns.events.a": "one"}
    assert svc.get_service_info()["event_patterns"] == ["ns.events.a"]

    svc._discover_handlers()  # a second call is a no-op, and changes nothing

    assert svc.container.registry.event_durables == {"ns.events.a": "one"}


# --- the registry's own snapshot and restore ----------------------------------


def _filled() -> ServiceRegistry:
    """A registry with something in every field, whatever fields it has."""
    registry = ServiceRegistry()
    for registered in fields(registry):
        collection = getattr(registry, registered.name)
        if isinstance(collection, dict):
            collection["k"] = "v"
        elif isinstance(collection, set):
            collection.add("k")
        else:
            collection.append("k")
    return registry


def test_restore_puts_back_every_field_the_registry_has():
    registry = _filled()
    before = registry.snapshot()
    expected = {name: type(value)(value) for name, value in before.items()}

    for registered in fields(registry):
        collection = getattr(registry, registered.name)
        if isinstance(collection, dict):
            collection["extra"] = "x"
        elif isinstance(collection, set):
            collection.add("extra")
        else:
            collection.append("extra")
    registry.restore(before)

    assert {
        registered.name: getattr(registry, registered.name) for registered in fields(registry)
    } == expected


def test_a_snapshot_is_a_copy_not_a_view_of_the_registry():
    registry = _filled()

    snapshot = registry.snapshot()
    registry.rpc_handlers["later"] = len

    assert "later" not in snapshot["rpc_handlers"]


def test_a_snapshot_names_every_field():
    registry = ServiceRegistry()

    assert set(registry.snapshot()) == {registered.name for registered in fields(registry)}

"""A handler decorated on an underscore-prefixed method is refused by name when discovery runs, not dropped silently.

Discovery skips every member whose name starts with an underscore. A `@listener`, `@rpc`,
`@timer`, `@validated_listener` or `@broadcast` on such a method therefore
registered nothing and raised nothing: the service started clean, the subject was never
subscribed, a durable was never created, and the author got no signal. Discovery (the first thing `start()` does
with the class) now refuses it, naming the method and the decorator. An underscore name stays legal for what discovery does not
read as a handler: a plain helper, and `@dependency`, whose probes are canonically `_check_db`.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from cliffracer import (
    CliffracerService,
    ConfigurationError,
    ServiceConfig,
    async_rpc,
    broadcast,
    dependency,
    listener,
    rpc,
    timer,
    validated_listener,
)

pytestmark = pytest.mark.unit


def _build(namespace: dict) -> CliffracerService:
    """The service after the discovery `start()` runs first, which is where members are read."""
    cls = type("Svc", (CliffracerService,), namespace)
    svc = cls(ServiceConfig(name="private_handler_svc", health_port=0))
    svc.container.discover_handlers()
    return svc


class _Event(BaseModel):
    n: int


def _decorated(kind: str):
    """One handler marker, and only that one, on a fresh function.

    A fresh function per case: decorators mutate the function they are given, so reusing one
    across cases lets `@listener` and `@broadcast` (or `@timer` and `@rpc`) mark the same
    function and cover for each other.
    """

    async def on_event(self, payload: _Event) -> None: ...
    async def on_rpc(self) -> dict:
        return {}

    async def on_tick(self) -> None: ...

    return {
        "listener": lambda: listener("private.events", fanout=True)(on_event),
        "rpc": lambda: rpc(on_rpc),
        "async_rpc": lambda: async_rpc(on_rpc),
        "timer": lambda: timer(60.0)(on_tick),
        "broadcast": lambda: broadcast("private.broadcast")(on_event),
    }[kind]()


@pytest.mark.parametrize("decorator", ["listener", "rpc", "async_rpc", "timer", "broadcast"])
def test_a_decorated_underscore_method_is_refused_by_name(decorator):
    with pytest.raises(ConfigurationError) as caught:
        _build({"_hidden": _decorated(decorator)})

    message = str(caught.value)
    assert "_hidden" in message and "Svc" in message, message
    assert "underscore" in message, message


def test_the_message_names_what_to_do():
    with pytest.raises(ConfigurationError, match="Rename it"):
        _build({"_hidden": _decorated("listener")})


def test_the_validated_listener_is_refused_too():
    async def on_valid(self, payload: _Event) -> None: ...

    with pytest.raises(ConfigurationError, match="_hidden"):
        _build({"_hidden": validated_listener("private.valid", _Event, fanout=True)(on_valid)})


def test_CONTROL_an_undecorated_underscore_helper_is_fine():
    async def _helper(self) -> int:
        return 1

    svc = _build({"_helper": _helper})
    assert list(svc.container.registry.rpc_handlers) == []


def test_CONTROL_a_dependency_probe_on_an_underscore_name_is_fine():
    @dependency("db", timeout=1.0)
    async def _check_db(self) -> dict:
        return {"ok": True}

    svc = _build({"_check_db": _check_db})
    assert [d.name for d in svc.container.registry.dependencies] == ["db"]


def test_CONTROL_the_same_handler_without_the_underscore_registers():
    svc = _build({"hidden": _decorated("listener")})
    assert list(svc.container.registry.event_handlers) != []

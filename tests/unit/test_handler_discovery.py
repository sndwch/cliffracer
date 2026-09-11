"""Tests ensuring handler discovery inspects classes rather than instances."""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import cliffracer.core.decorators as decorators_module
import cliffracer.core.extension as extension_module
from cliffracer import CliffracerService, ServiceConfig, listener, rpc


class _Svc(CliffracerService):
    """One of each handler kind, so discovery has something real to find."""

    @rpc
    async def do_thing(self, value: str) -> str:
        return value

    @listener("orders.created", fanout=True)
    async def on_order(self, subject: str) -> None:
        pass


def _config(**overrides):
    return ServiceConfig(name="svc", namespace="app1", **overrides)


@pytest.mark.unit
def test_a_mock_attached_before_discovery_is_not_registered():
    """The original defect: an AsyncMock on self answered every hasattr."""
    svc = _Svc(_config())
    svc.nc = AsyncMock()
    svc.js = AsyncMock()

    svc._discover_handlers()

    assert set(svc.container.registry.rpc_handlers) == {"do_thing"}
    assert set(svc.container.registry.event_handlers) == {"app1.orders.created"}
    assert not any(
        isinstance(h, AsyncMock | MagicMock) for h in svc.container.registry.event_handlers.values()
    )
    assert not any(
        isinstance(h, AsyncMock | MagicMock) for h in svc.container.registry.rpc_handlers.values()
    )


@pytest.mark.unit
def test_an_arbitrary_marked_object_on_self_is_not_registered():
    """Not just Mocks -- nothing assigned to self is a candidate at all."""

    class Marked:
        _cliffracer_rpc = True
        _cliffracer_events = ["sneaky.subject"]

    svc = _Svc(_config())
    svc.impostor = Marked()

    svc._discover_handlers()

    assert "impostor" not in svc.container.registry.rpc_handlers
    assert "app1.sneaky.subject" not in svc.container.registry.event_handlers
    assert set(svc.container.registry.event_handlers) == {"app1.orders.created"}


@pytest.mark.unit
def test_discovery_is_order_independent():
    """Verify attaching transport mocks before or after discovery produces identical registration."""
    before = _Svc(_config())
    before.nc, before.js = AsyncMock(), AsyncMock()
    before._discover_handlers()

    after = _Svc(_config())
    after._discover_handlers()
    after.nc, after.js = AsyncMock(), AsyncMock()

    assert set(before.container.registry.rpc_handlers) == set(after.container.registry.rpc_handlers)
    assert set(before.container.registry.event_handlers) == set(
        after.container.registry.event_handlers
    )
    assert set(before.container.registry.event_durables) == set(
        after.container.registry.event_durables
    )


@pytest.mark.unit
def test_a_public_property_is_not_evaluated_during_discovery():
    """Verify public properties on service classes are not evaluated during discovery."""
    evaluated = []

    class WithProperty(_Svc):
        @property
        def dangerous(self):
            evaluated.append("yes")
            raise RuntimeError("property evaluated during discovery")

    svc = WithProperty(_config())
    svc._discover_handlers()  # must not raise

    assert evaluated == []
    assert set(svc.container.registry.rpc_handlers) == {"do_thing"}


@pytest.mark.unit
def test_handlers_defined_on_base_classes_are_still_found():
    """Class scanning must walk the MRO, not just the leaf class."""

    class Mixin:
        @rpc
        async def from_mixin(self, value: str) -> str:
            return value

    class Child(Mixin, _Svc):
        @rpc
        async def from_child(self, value: str) -> str:
            return value

    svc = Child(_config())
    svc._discover_handlers()

    assert {"do_thing", "from_mixin", "from_child"} <= set(svc.container.registry.rpc_handlers)
    assert "app1.orders.created" in svc.container.registry.event_handlers


@pytest.mark.unit
def test_a_subclass_override_wins_over_the_base_definition():
    """Overriding a handler must register the subclass's implementation."""

    class Child(_Svc):
        @rpc
        async def do_thing(self, value: str) -> str:
            return "overridden"

    svc = Child(_config())
    svc._discover_handlers()

    assert svc.container.registry.rpc_handlers["do_thing"].__func__ is Child.do_thing


@pytest.mark.unit
def test_every_marker_producer_uses_the_discovered_prefix():
    """Guard against drift between the marker PRODUCERS and the discovery gate.

    Discovery gates on ``_HANDLER_MARKER_PREFIX`` rather than an explicit list
    so that a new marker cannot silently produce an undiscoverable handler.
    This remains true as long as every producer keeps using the prefix.

    There are two producers: the decorators, which assign the marker directly,
    and ``core/extension.py``'s ``entrypoint`` helper, which appends through
    ``func.__dict__.setdefault``.

    The presence assertion ensures both producers remain in the file list.
    pins the file list; the prefix assertion pins the naming.
    """
    sources = [
        Path(decorators_module.__file__).read_text(),
        Path(extension_module.__file__).read_text(),
    ]
    patterns = (r"func\.(_[a-z_]+)\s*=", r'func\.__dict__\.setdefault\(\s*"(_[a-z_]+)"')
    markers: set[str] = set()
    for source in sources:
        for pattern in patterns:
            markers |= set(re.findall(pattern, source))

    assert markers, "no marker assignments found -- have the producers moved?"
    assert all(m.startswith(CliffracerService._HANDLER_MARKER_PREFIX) for m in markers), (
        f"markers not covered by the discovery gate: "
        f"{sorted(m for m in markers if not m.startswith(CliffracerService._HANDLER_MARKER_PREFIX))}"
    )
    assert "_cliffracer_entrypoints" in markers, (
        "the entrypoint marker was not found -- is core/extension.py still in the "
        f"file list, and does its setdefault form still match? found: {sorted(markers)}"
    )


@pytest.mark.unit
def test_a_non_string_event_subject_is_refused_at_registration():
    """Verify non-string event subject raises TypeError at registration time."""
    svc = _Svc(_config())

    with pytest.raises(TypeError) as exc:
        svc._register_event_handler(object(), svc.on_order)

    message = str(exc.value)
    assert "must be a str" in message
    assert "on_order" in message


from cliffracer.core.typed_rpc import UntypedHandler  # noqa: E402


@pytest.mark.unit
def test_discovery_builds_a_spec_per_rpc_handler():
    class Svc(CliffracerService):
        @rpc
        async def echo(self, text: str) -> str:
            return text

    svc = Svc(ServiceConfig(name="d"))
    svc._discover_handlers()
    assert set(svc.container.registry.rpc_specs) == {"echo"}
    assert [p.name for p in svc.container.registry.rpc_specs["echo"].params] == ["text"]


@pytest.mark.unit
def test_an_unannotated_handler_makes_discovery_refuse_by_name():
    class Svc(CliffracerService):
        @rpc
        async def echo(self, text):
            return text

    svc = Svc(ServiceConfig(name="d"))
    with pytest.raises(UntypedHandler, match=r"Svc\.echo.*'text'"):
        svc._discover_handlers()


@pytest.mark.unit
async def test_start_raises_before_connecting_on_an_untyped_handler(monkeypatch):
    class Svc(CliffracerService):
        @rpc
        async def echo(self, text: str):
            return text

    svc = Svc(ServiceConfig(name="d", nats_url="nats://127.0.0.1:1"))
    connected = []
    monkeypatch.setattr(svc.container, "connect", lambda *a, **k: connected.append(1))
    with pytest.raises(UntypedHandler, match="return"):
        await svc.start()
    assert connected == []

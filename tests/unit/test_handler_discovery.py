"""Tests ensuring handler discovery inspects classes rather than instances."""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, listener, rpc
from cliffracer.core.discovery import HandlerDiscovery

pytestmark = pytest.mark.unit


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


class _DurableSvc(_Svc):
    """`_Svc` plus a durable listener, so the durables are not an empty set on both sides."""

    @listener("orders.audited", durable="auditor")
    async def on_audit(self, subject: str) -> None:
        pass


def test_discovery_is_order_independent():
    """Verify attaching transport mocks before or after discovery produces identical registration."""
    before = _DurableSvc(_config(jetstream_enabled=True))
    before.nc, before.js = AsyncMock(), AsyncMock()
    before._discover_handlers()

    after = _DurableSvc(_config(jetstream_enabled=True))
    after._discover_handlers()
    after.nc, after.js = AsyncMock(), AsyncMock()

    # Each side is held to what discovery should have registered. Comparing the
    # two registries to each other alone is satisfied by two empty ones.
    for svc in (before, after):
        registry = svc.container.registry
        assert set(registry.rpc_handlers) == {"do_thing"}
        assert set(registry.event_handlers) == {"app1.orders.created", "app1.orders.audited"}
        assert registry.event_durables == {"app1.orders.audited": "auditor"}

    assert set(before.container.registry.rpc_handlers) == set(after.container.registry.rpc_handlers)
    assert set(before.container.registry.event_handlers) == set(
        after.container.registry.event_handlers
    )
    assert before.container.registry.event_durables == after.container.registry.event_durables


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


def test_a_subclass_override_wins_over_the_base_definition():
    """Overriding a handler must register the subclass's implementation."""

    class Child(_Svc):
        @rpc
        async def do_thing(self, value: str) -> str:
            return "overridden"

    svc = Child(_config())
    svc._discover_handlers()

    assert svc.container.registry.rpc_handlers["do_thing"].__func__ is Child.do_thing


#: Names a decorated callable is handed as it is wrapped, wherever a marker is written.
_DECORATED_NAMES = {
    "func", "m", "wrapper", "async_wrapper", "sync_wrapper",
    "method", "fn", "f", "handler", "decorated",
}  # fmt: skip

#: Attributes written the same way that are not markers, with the reason each is exempt. None now:
#: `_rate_limit`, an alias of `_cliffracer_rate_limit` that nothing read, is no longer written.
_NOT_MARKERS: set[str] = set()

#: Markers the scan must find. A scan that finds none of them is reading the wrong files.
_KNOWN_MARKERS = {
    "_cliffracer_rpc",
    "_cliffracer_events",
    "_cliffracer_timers",
    "_cliffracer_rate_limit",
    "_cliffracer_dependency",
    "_cliffracer_idempotent",
}


def _marker_writes(tree: ast.AST, prefix: str) -> list[tuple[str, int]]:
    """Private attribute names written in `tree` that may be markers, as (name, line).

    Three syntactic forms: `X._x = ...`, `setattr(X, "_x", ...)` and
    `X.__dict__.setdefault("_x", ...)`. A write is read when `X` is one of the names a decorator
    gives the callable it is wrapping, or, whatever `X` is, when the name carries the marker
    prefix: a decorator that calls its callable `target` still writes `_cliffracer_rpc` where this
    sees it. A name without the prefix written on some other receiver cannot be told from any
    other private attribute, so it is read on the listed names only.
    """
    writes: list[tuple[str, int]] = []

    def receiver_is_decorated(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in _DECORATED_NAMES

    def record(name: str, receiver: ast.AST, node: ast.AST) -> None:
        if not name.startswith("_") or name.startswith("__"):
            return
        if name.startswith(prefix) or receiver_is_decorated(receiver):
            writes.append((name, node.lineno))  # type: ignore[attr-defined]

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    record(target.attr, target.value, node)
        elif isinstance(node, ast.Call):
            func, args = node.func, node.args
            if (
                isinstance(func, ast.Name)
                and func.id == "setattr"
                and len(args) >= 2
                and isinstance(args[1], ast.Constant)
                and isinstance(args[1].value, str)
            ):
                record(args[1].value, args[0], node)
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "setdefault"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "__dict__"
                and args
                and isinstance(args[0], ast.Constant)
                and isinstance(args[0].value, str)
            ):
                record(args[0].value, func.value.value, node)
    return writes


def _attribute_names_written_on_decorated_callables() -> dict[str, set[str]]:
    """Every marker write `_marker_writes` finds in the shipped sources, as name -> `path:line`."""
    repo = Path(__file__).resolve().parents[2]
    roots = [repo / "src" / "cliffracer", *sorted((repo / "packages").glob("*/src"))]
    prefix = HandlerDiscovery.HANDLER_MARKER_PREFIX
    # An empty prefix would read every private attribute on every receiver as a marker.
    assert prefix.startswith("_") and len(prefix) > 1, f"the marker prefix is {prefix!r}"
    written: dict[str, set[str]] = {}
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            for name, line in _marker_writes(ast.parse(path.read_text()), prefix):
                written.setdefault(name, set()).add(f"{path.relative_to(repo)}:{line}")
    return written


def test_every_marker_producer_uses_the_discovered_prefix():
    """Guard against drift between the marker PRODUCERS and the discovery gate.

    Discovery decides by `HandlerDiscovery.HANDLER_MARKER_PREFIX` (the gate in `discover`), so
    that a new marker cannot silently produce an undiscoverable handler. This reads that
    constant, and reads every producer in the shipped sources rather than a fixed pair of
    files: decorators, timers, dependencies, idempotency, dispatch
    metadata and the resilience package's rate limiter.
    """
    prefix = HandlerDiscovery.HANDLER_MARKER_PREFIX
    written = _attribute_names_written_on_decorated_callables()

    assert _KNOWN_MARKERS <= set(written), (
        "the scan did not find markers that are written: have the producers moved? "
        f"missing {sorted(_KNOWN_MARKERS - set(written))}"
    )
    assert _NOT_MARKERS <= set(written), (
        f"an exemption nothing writes any more should be removed: {sorted(_NOT_MARKERS - set(written))}"
    )
    drifted = {
        name: sorted(sites)
        for name, sites in written.items()
        if not name.startswith(prefix) and name not in _NOT_MARKERS
    }
    assert not drifted, f"markers not covered by the discovery gate {prefix!r}: {drifted}"


def _planted(source: str) -> list[tuple[str, int]]:
    return _marker_writes(ast.parse(source), HandlerDiscovery.HANDLER_MARKER_PREFIX)


@pytest.mark.parametrize(
    ("source", "found"),
    [
        pytest.param("target._cliffracer_rpc = True\n", [("_cliffracer_rpc", 1)], id="assigned"),
        pytest.param(
            'setattr(obj.inner, "_cliffracer_events", [])\n',
            [("_cliffracer_events", 1)],
            id="setattr-on-an-attribute",
        ),
        pytest.param(
            'wrapped.__dict__.setdefault("_cliffracer_timers", [])\n',
            [("_cliffracer_timers", 1)],
            id="dict-setdefault",
        ),
        pytest.param(
            "make()._cliffracer_dependency: object = None\n",
            [("_cliffracer_dependency", 1)],
            id="annotated-on-a-call",
        ),
    ],
)
def test_CONTROL_a_prefixed_marker_is_read_whatever_its_receiver_is_called(source, found):
    """A decorator whose inner `mark()` names the callable it marks `target`, a name not in the
    listed set, writes `target._cliffracer_rpc`; read only on the listed names, that known marker
    would be reported missing."""
    assert _planted(source) == found


def test_CONTROL_what_the_receiver_decides_is_still_decided_by_it():
    """An unprefixed name is read on a listed receiver, so a producer that drifts off the prefix
    is still found there, and not on any other receiver, where it is just a private attribute.
    A dunder is never read."""
    source = "func._rate = 1\nself._cache = {}\nfunc.__wrapped__ = other\n"

    assert _planted(source) == [("_rate", 1)]


def test_the_service_class_carries_the_same_marker_prefix_as_the_gate():
    """`CliffracerService._HANDLER_MARKER_PREFIX` is read by nothing, so it can only be kept
    honest by being compared with the constant that decides."""
    assert CliffracerService._HANDLER_MARKER_PREFIX == HandlerDiscovery.HANDLER_MARKER_PREFIX


def test_a_non_string_event_subject_is_refused_by_discovery():
    """A listener whose pattern is not a string is refused by `_discover_handlers`, by name.

    The decorator checks its own argument, so the marker is written by hand here to reach the
    check discovery makes of whatever a marker holds.
    """

    class Bad(CliffracerService):
        async def on_order(self, subject: str) -> None:
            pass

    Bad.on_order._cliffracer_events = [object()]  # type: ignore[attr-defined]
    svc = Bad(_config())

    with pytest.raises(TypeError) as exc:
        svc._discover_handlers()

    message = str(exc.value)
    assert "event subject must be a str" in message
    assert "on_order" in message


from cliffracer.core.typed_rpc import UntypedHandler  # noqa: E402


def test_discovery_builds_a_spec_per_rpc_handler():
    class Svc(CliffracerService):
        @rpc
        async def echo(self, text: str) -> str:
            return text

    svc = Svc(ServiceConfig(name="d"))
    svc._discover_handlers()
    assert set(svc.container.registry.rpc_specs) == {"echo"}
    assert [p.name for p in svc.container.registry.rpc_specs["echo"].params] == ["text"]


def test_an_unannotated_handler_makes_discovery_refuse_by_name():
    class Svc(CliffracerService):
        @rpc
        async def echo(self, text):
            return text

    svc = Svc(ServiceConfig(name="d"))
    with pytest.raises(UntypedHandler, match=r"Svc\.echo.*'text'"):
        svc._discover_handlers()


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


def test_a_decorated_callable_assigned_to_self_is_not_a_handler():
    """The case a scan of the INSTANCE would reintroduce.

    A bound method stored on `self` carries the markers of the function it wraps, so an
    instance scan registers it; the class scan does not see it, because it is not a member of
    the class. The tests above pass under an instance scan for incidental reasons (a mock's
    `__dict__` has no markers; a class attribute is not in an instance's `__dict__`).
    """

    class Holder:
        @rpc
        async def stored(self, value: str) -> str:
            return value

    svc = _Svc(_config())
    svc.stored = Holder().stored  # type: ignore[attr-defined]

    svc._discover_handlers()

    assert set(svc.container.registry.rpc_handlers) == {"do_thing"}


def _undeclared_fanout():
    class S(CliffracerService):
        @listener("events.thing")
        async def on_thing(self, subject: str) -> None:
            pass

    return S, "declare neither a durable nor fanout"


def _inert_durable():
    class S(CliffracerService):
        @listener("events.thing", durable="d1")
        async def on_thing(self, subject: str) -> None:
            pass

    return S, "inert"


def _pull_without_a_durable():
    class S(CliffracerService):
        @listener("events.thing", pull=True)
        async def on_thing(self, subject: str) -> None:
            pass

    return S, "pull=True"


def _a_duplicate_subject():
    class S(CliffracerService):
        @listener("events.thing", fanout=True)
        async def first(self, subject: str) -> None:
            pass

        @listener("events.thing", fanout=True)
        async def second(self, subject: str) -> None:
            pass

    return S, "Duplicate event listener"


@pytest.mark.parametrize(
    "build",
    [_undeclared_fanout, _inert_durable, _pull_without_a_durable, _a_duplicate_subject],
)
async def test_each_discovery_refusal_is_raised_by_start_before_the_broker_is_touched(
    build, monkeypatch
):
    """The ADR's promise, per error class: an operator sees the refusal before the process
    connects or runs `on_startup`. Every other test calls `_discover_handlers()` directly, so
    moving discovery after the connect would have left them green."""
    service_class, expected = build()
    started: list[str] = []

    class Svc(service_class):  # type: ignore[valid-type, misc]
        async def on_startup(self) -> None:
            started.append("on_startup")

    svc = Svc(ServiceConfig(name="d"))
    connected: list[int] = []
    monkeypatch.setattr(svc.container, "connect", lambda *a, **k: connected.append(1))

    with pytest.raises(ConfigurationError, match=expected):
        await svc.start()

    assert connected == [], "start() connected before discovery refused"
    assert started == [], "start() ran on_startup before discovery refused"

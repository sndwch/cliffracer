"""A subclass that overrides a decorated handler without the decorator is warned about, once.

Discovery reads the markers off the member the class resolves, so an override with no decorator
registers nothing: the inherited handler is silently gone, the service starts, and nothing says so
(the same shape as the silent failures discovery otherwise refuses). It can be deliberate, so it is
a WARNING naming the class, the base and the handler, not a refusal; to keep the handler, put the
decorator on the override. The behaviour itself is unchanged and is pinned in
`test_duplicate_listener_and_harness_stress.py`.
"""

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, listener, rpc

pytestmark = pytest.mark.unit


def _discover_and_collect_warnings(service_class) -> tuple[CliffracerService, list[str]]:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        svc = service_class(ServiceConfig(name="override_svc", health_port=0))
        svc._discover_handlers()
    finally:
        logger.remove(sink)
    return svc, [line for line in lines if "overrides" in line]


def test_an_override_of_a_decorated_listener_without_the_decorator_is_warned_about():
    class Base(CliffracerService):
        @listener("payments.processed", fanout=True)
        async def on_payment(self, subject: str) -> None:
            pass

    class Derived(Base):
        async def on_payment(self, subject: str) -> None:
            pass

    svc, warnings = _discover_and_collect_warnings(Derived)

    assert len(svc.container.registry.event_handlers) == 0  # the behaviour is unchanged
    (line,) = warnings
    assert "Derived.on_payment overrides Base.on_payment" in line, line
    assert "@listener" in line and "put the decorator on Derived.on_payment" in line, line


def test_an_override_of_a_decorated_rpc_without_the_decorator_is_warned_about():
    class Base(CliffracerService):
        @rpc
        async def work(self, value: str) -> str:
            return value

    class Derived(Base):
        async def work(self, value: str) -> str:
            return value.upper()

    svc, warnings = _discover_and_collect_warnings(Derived)

    assert "work" not in svc.container.registry.rpc_handlers
    (line,) = warnings
    assert "Derived.work overrides Base.work" in line and "@rpc" in line, line


def test_the_warning_names_the_nearest_decorated_base_through_an_undecorated_middle():
    class Base(CliffracerService):
        @listener("payments.processed", fanout=True)
        async def on_payment(self, subject: str) -> None:
            pass

    class Middle(Base):
        async def on_payment(self, subject: str) -> None:
            pass

    class Derived(Middle):
        async def on_payment(self, subject: str) -> None:
            pass

    _, warnings = _discover_and_collect_warnings(Derived)

    (line,) = warnings
    assert "Derived.on_payment overrides Base.on_payment" in line, line


def test_CONTROL_an_override_that_repeats_the_decorator_is_not_warned_about():
    class Base(CliffracerService):
        @listener("payments.processed", fanout=True)
        async def on_payment(self, subject: str) -> None:
            pass

    class Derived(Base):
        @listener("payments.processed", fanout=True)
        async def on_payment(self, subject: str) -> None:
            pass

    svc, warnings = _discover_and_collect_warnings(Derived)

    assert warnings == []
    assert "payments.processed" in svc.container.registry.event_handlers


def test_CONTROL_an_inherited_handler_that_is_not_overridden_is_not_warned_about():
    class Base(CliffracerService):
        @listener("payments.processed", fanout=True)
        async def on_payment(self, subject: str) -> None:
            pass

    class Derived(Base):
        async def something_else(self) -> None:
            pass

    svc, warnings = _discover_and_collect_warnings(Derived)

    assert warnings == []
    assert "payments.processed" in svc.container.registry.event_handlers


def test_CONTROL_overriding_a_method_that_was_never_a_handler_is_not_warned_about():
    class Base(CliffracerService):
        async def helper(self) -> None:
            pass

    class Derived(Base):
        async def helper(self) -> None:
            pass

    _, warnings = _discover_and_collect_warnings(Derived)

    assert warnings == []


def test_it_is_warned_once_per_discovery_not_once_per_call():
    class Base(CliffracerService):
        @listener("payments.processed", fanout=True)
        async def on_payment(self, subject: str) -> None:
            pass

    class Derived(Base):
        async def on_payment(self, subject: str) -> None:
            pass

    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        svc = Derived(ServiceConfig(name="once", health_port=0))
        svc._discover_handlers()
        svc._discover_handlers()  # discovery is done once; a second call does nothing
    finally:
        logger.remove(sink)

    assert len([line for line in lines if "overrides" in line]) == 1

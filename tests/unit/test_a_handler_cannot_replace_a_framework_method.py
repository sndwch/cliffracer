"""A decorated handler named for a method the service already has is refused when it starts."""

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc, timer
from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


def _discover(service: CliffracerService) -> None:
    service.container.discover_handlers()


@pytest.mark.parametrize(
    "name", ["health_check", "liveness_check", "is_live", "get_service_info", "publish_event"]
)
@pytest.mark.parametrize(
    "decorate",
    [timer(interval=30), rpc, listener("x.y", fanout=True)],
    ids=["timer", "rpc", "listener"],
)
def test_a_handler_named_for_a_framework_method_is_refused_naming_both(name, decorate):
    async def handler(self) -> None:
        return None

    handler.__name__ = name
    service = type("Svc", (CliffracerService,), {name: decorate(handler)})(ServiceConfig(name="s"))

    with pytest.raises(ConfigurationError) as caught:
        _discover(service)

    message = str(caught.value)
    assert f"Svc.{name}" in message and f"{name!r}" in message, message
    assert "Give the handler another name" in message, message


def test_the_probe_a_timer_named_health_check_would_have_broken_is_not_left_replaced():
    """The scenario: the timer returned None, and /health read `.get` on it."""

    class Svc(CliffracerService):
        @timer(interval=30)
        async def health_check(self) -> None:
            pass

    with pytest.raises(ConfigurationError, match="health_check"):
        _discover(Svc(ServiceConfig(name="s")))


def test_CONTROL_a_handler_with_its_own_name_is_accepted():
    class Svc(CliffracerService):
        @timer(interval=30)
        async def check_dependencies(self) -> None:
            pass

        @rpc
        async def ping(self) -> str:
            return "pong"

    service = Svc(ServiceConfig(name="s"))
    _discover(service)

    assert [t.method_name for t in service.container.registry.timers] == ["check_dependencies"]


def test_CONTROL_an_undecorated_override_of_a_framework_method_is_not_a_handler():
    """Overriding `health_check` is how a service adds its own readiness; only a handler is refused."""

    class Svc(CliffracerService):
        async def health_check(self) -> dict[str, str]:
            return {"status": "healthy"}

    _discover(Svc(ServiceConfig(name="s")))

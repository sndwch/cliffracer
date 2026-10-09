"""A handler named `config`, `logger` or `health_listener` is refused, and a bad dead-letter template is a `ValidationError`.

`CliffracerService.__init__` sets `config`, `logger` and `health_listener` on the instance. The
refusal of a handler named for something the framework calls read class attributes only, so it
did not see those three: `describe` published the method, and `discover` found the instance
attribute where the method should have been, saw no handler marker on it, and registered nothing.
The service started, the generated client had a `config` method, and calling it got no responder.
The refusal now reads the names off a constructed service, as `reserved_rpc_method_names` does for
a client.

The same file holds the other bare exception at config construction: a `dlq_subject` template that
raised something other than `KeyError` or `IndexError` while rendering escaped `ServiceConfig(...)`
as that exception, where every other bad template is a `ValidationError`.
"""

import pytest
from pydantic import ValidationError

from cliffracer import CliffracerService, ServiceConfig, listener, rpc, timer
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit

SHADOWED = ["config", "logger", "health_listener"]


def discovered(service):
    return HandlerDiscovery.discover(service, service.config)


def service_with(kind: str, name: str):
    def handler(self, *args):
        return None

    namespace = {
        "__init__": lambda self: CliffracerService.__init__(
            self, ServiceConfig(name="svc", subject_prefix=None, health_port=0)
        )
    }
    if kind == "rpc":

        async def rpc_handler(self) -> int:
            return 1

        rpc_handler.__name__ = name
        namespace[name] = rpc(rpc_handler)
    elif kind == "timer":

        async def timer_handler(self) -> None:
            return None

        timer_handler.__name__ = name
        namespace[name] = timer(interval=60)(timer_handler)
    else:

        async def listener_handler(self, subject: str) -> None:
            return None

        listener_handler.__name__ = name
        namespace[name] = listener("things.created")(listener_handler)
    return type("Svc", (CliffracerService,), namespace)


@pytest.mark.parametrize("name", SHADOWED)
@pytest.mark.parametrize("kind", ["rpc", "timer", "listener"])
def test_discover_refuses_a_handler_an_instance_attribute_would_shadow(kind, name):
    service = service_with(kind, name)()

    with pytest.raises(ConfigurationError) as caught:
        discovered(service)

    assert f"Svc.{name}" in str(caught.value), str(caught.value)
    assert "Give the handler another name" in str(caught.value)


@pytest.mark.parametrize("name", SHADOWED)
def test_describe_refuses_it_too_so_it_never_publishes_what_discovery_cannot_serve(name):
    with pytest.raises(ConfigurationError, match=f"Svc.{name}"):
        describe(service_with("rpc", name))


def test_CONTROL_the_names_come_off_the_constructor_not_a_list():
    assert set(SHADOWED) <= HandlerDiscovery._attributes_a_service_sets_in_its_constructor()
    assert "process" not in HandlerDiscovery._attributes_a_service_sets_in_its_constructor()


def test_an_attribute_added_to_the_constructor_is_reserved_with_it(monkeypatch):
    """The names are read off a constructed service, so one the constructor starts setting is
    reserved without a list to update."""
    real_init = CliffracerService.__init__

    def init_with_one_more(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        self.added_in_the_constructor = 1

    monkeypatch.setattr(CliffracerService, "__init__", init_with_one_more)
    HandlerDiscovery._attributes_a_service_sets_in_its_constructor.cache_clear()
    try:
        reserved = HandlerDiscovery._attributes_a_service_sets_in_its_constructor()
    finally:
        HandlerDiscovery._attributes_a_service_sets_in_its_constructor.cache_clear()

    assert "added_in_the_constructor" in reserved


def test_CONTROL_an_ordinary_handler_name_is_discovered():
    registry = HandlerDiscovery.discover(
        *(lambda s: (s, s.config))(service_with("rpc", "process")())
    )

    assert "process" in registry.rpc_handlers


@pytest.mark.parametrize(
    "template",
    ["dlq.{service.nope}", "dlq.{service.upper()}", "dlq.{service[0].x}", "dlq.{namespace.nope}"],
)
def test_a_dlq_template_that_raises_while_rendering_is_a_validation_error(template):
    with pytest.raises(ValidationError) as caught:
        ServiceConfig(name="orders", subject_prefix=None, health_port=0, dlq_subject=template)

    assert "dlq_subject cannot be rendered" in str(caught.value)
    assert template in str(caught.value)


@pytest.mark.parametrize("template", ["dlq.{service", "dlq.{}", "dlq.{service!z}", "dlq.{0}"])
def test_CONTROL_the_templates_that_were_already_validation_errors_still_are(template):
    with pytest.raises(ValidationError):
        ServiceConfig(name="orders", subject_prefix=None, health_port=0, dlq_subject=template)


def test_CONTROL_a_good_template_still_renders():
    config = ServiceConfig(
        name="orders", subject_prefix=None, health_port=0, dlq_subject="dlq.{service}"
    )

    assert HandlerDiscovery.dlq_subject(config) == "dlq.orders"

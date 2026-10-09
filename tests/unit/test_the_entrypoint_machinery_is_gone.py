"""The extension entrypoint machinery is not part of the framework.

`Extension.entrypoint_kinds()`, the `entrypoint` decorator, the container's kind map and the
per-kind binders in discovery existed so a transport extension could register route and
websocket entrypoints. The last such extension left with cliffracer-http, and nothing in the
tree declared a kind after that: a decorator no shipped code used, and a container map nothing
filled. These tests pin that the surface is gone, so it does not come back without a user.

What an extension is still given is unchanged: the hooks, `bind`, and `_origin`.
"""

import inspect

import pytest

import cliffracer
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core import extension
from cliffracer.core.container import Container
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.extension import Extension
from cliffracer.runners.templates import TemplateCatalog

pytestmark = pytest.mark.unit


def test_the_decorator_is_not_exported():
    assert "entrypoint" not in cliffracer.__all__
    assert "entrypoint" not in extension.__all__
    assert not hasattr(cliffracer, "entrypoint")
    assert not hasattr(extension, "entrypoint")


def test_an_extension_has_no_entrypoint_kinds_hook():
    assert not hasattr(Extension, "entrypoint_kinds")


def test_the_container_holds_no_kind_map():
    svc = CliffracerService(ServiceConfig(name="s"))

    assert not hasattr(svc.container, "_entrypoint_kinds")
    assert "entrypoint" not in inspect.getsource(Container)


def test_discovery_takes_no_kind_map_and_reads_no_entrypoint_marker():
    assert "entrypoint_kinds" not in inspect.signature(HandlerDiscovery.discover).parameters
    assert "_cliffracer_entrypoints" not in HandlerDiscovery._HANDLER_DECORATORS
    assert "entrypoint" not in inspect.getsource(HandlerDiscovery).lower()


def test_an_extension_that_still_defines_the_old_hook_is_built_and_ignored():
    """A subclass written for the old contract keeps working; the method is never called."""
    called = []

    class Leftover(Extension):
        def entrypoint_kinds(self):
            called.append("read")
            return {"route": lambda *a: None}

    class Svc(CliffracerService):
        leftover = Leftover()

    svc = Svc(ServiceConfig(name="s"))
    svc._discover_handlers()

    assert called == []
    assert svc.leftover._origin is Svc.leftover


def test_a_template_accepts_an_extension_that_still_defines_the_old_hook():
    """Templates refused such an extension by the hook's name; the hook no longer exists to refuse."""
    from tests.fixtures.shipment_templates import Shipments, shipment_template

    class Leftover(Extension):
        def entrypoint_kinds(self):
            return {}

    class Freight(Shipments):
        legacy = Leftover()

    TemplateCatalog().register(shipment_template(service_class=Freight))

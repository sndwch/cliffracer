"""An extension passed as another extension's argument is built fresh for every bound instance.

The extensions guide says so next to the rules for callables and uncopyable arguments, so a
declaration `Outer(inner=Inner())` gives two services two `Inner` objects, not one shared one.
"""

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


class Inner(Extension):
    pass


class Outer(Extension):
    def __init__(self, inner: Inner) -> None:
        self.inner = inner


class Svc(CliffracerService):
    outer = Outer(inner=Inner())


def _service(name: str) -> Svc:
    return Svc(ServiceConfig(name=name, health_port=0))


def test_two_services_get_two_inner_extensions():
    first, second = _service("one"), _service("two")

    assert isinstance(first.outer.inner, Inner) and isinstance(second.outer.inner, Inner)
    assert first.outer.inner is not second.outer.inner


def test_neither_is_the_inner_extension_the_declaration_holds():
    declared = Svc.__dict__["outer"]

    service = _service("three")

    assert service.outer.inner is not declared._spec_kwargs["inner"]

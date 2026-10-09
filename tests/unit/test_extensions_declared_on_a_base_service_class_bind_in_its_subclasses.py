"""An extension declared on a base service class is bound in every subclass, base declarations first.

`CliffracerService._collect_extensions` walks the class's MRO from the root down, so an extension
declared on a shared base service reaches each subclass that inherits from it, a subclass that
declares the same attribute name replaces the base's rather than adding a second, and what a base
declares is set up before what a subclass adds. Every other test declares its extensions on the
concrete class, so a change that scanned only `vars(type(self))` kept the whole suite green while
every service with a shared base lost its extensions at start, with no error.
"""

from __future__ import annotations

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension
from tests.conftest import declared

pytestmark = pytest.mark.unit


class Tagged(Extension):
    """An extension that records which class declared it, in the order the container set it up."""

    def __init__(self, tag: str):
        self.tag = tag

    async def setup(self, ctx):
        ctx.service.setup_order.append(self.tag)


class Base(CliffracerService):
    audit = Tagged("base.audit")
    shared = Tagged("base.shared")

    def __init__(self, config):
        self.setup_order: list[str] = []
        super().__init__(config)


class Child(Base):
    extra = Tagged("child.extra")


class Replacing(Base):
    shared = Tagged("replacing.shared")


class GrandChild(Child):
    late = Tagged("grandchild.late")


def _service(cls):
    return cls(ServiceConfig(name="inherit", health_port=0))


def test_a_base_declaration_binds_in_a_subclass_that_declares_nothing_itself():
    class Bare(Base):
        pass

    assert declared(_service(Bare)) == ["audit", "shared"]


def test_a_subclass_has_the_base_declarations_and_its_own_with_the_base_first():
    assert declared(_service(Child)) == ["audit", "shared", "extra"]


def test_a_declaration_is_inherited_through_every_level():
    assert declared(_service(GrandChild)) == ["audit", "shared", "extra", "late"]


def test_a_subclass_redeclaring_a_name_replaces_the_base_one_instead_of_adding_a_second():
    svc = _service(Replacing)

    assert declared(svc).count("shared") == 1
    assert svc.shared.tag == "replacing.shared"
    assert "base.shared" not in [e.tag for e in svc.extensions if isinstance(e, Tagged)]


async def test_what_a_base_declares_is_set_up_before_what_a_subclass_adds():
    svc = _service(GrandChild)

    await svc.container._setup_extensions()

    assert svc.setup_order == ["base.audit", "base.shared", "child.extra", "grandchild.late"]


def test_each_subclass_instance_gets_its_own_bound_copy_of_an_inherited_declaration():
    first, second = _service(Child), _service(Child)

    assert first.audit is not second.audit
    assert first.audit is not Base.audit
    assert first.audit.service is first and second.audit.service is second


def test_CONTROL_a_subclass_does_not_leak_its_declaration_into_its_base():
    assert declared(_service(Base)) == ["audit", "shared"]
    assert declared(_service(Replacing)) == ["audit", "shared"]
    assert _service(Base).shared.tag == "base.shared"

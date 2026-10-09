"""A declared extension specification is frozen, and freezing it twice is harmless.

ADR-0005: extension attributes declared on a service class are immutable
factory specifications, and mutating one fails loudly. The guards that make it
fail are armed by `Extension.freeze()`, so the promise holds only if the
framework calls it. These tests declare and bind specifications the way a user
does rather than calling `freeze()` themselves: a test that freezes a spec by
hand proves the guards work in a state it put the spec in, not that a user's
spec ever reaches it.

Why a write must raise rather than be tolerated: every runtime instance is
built from the constructor arguments captured when the spec was constructed,
never from its attributes, so a write to a spec reaches no service. Before a
service exists or after, it vanished without a sign.

Each test names the half it pins:

- FROZEN: a spec is frozen when a class body declares it, and a spec passed to
  `add_extension` when it is bound.
- IDEMPOTENT: `freeze()` on a frozen spec is a no-op. The bind path depends on
  it: a class-declared spec is shared by every instance of the class and is
  frozen again each time one is built.

Every test declares its own service class, so a write that a broken build lets
through cannot leak into the next test through a shared spec.
"""

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit

REFUSED = "immutable extension specification"


class Settings(Extension):
    def __init__(self, ttl: int = 5):
        self.ttl = ttl


def _declare():
    class Svc(CliffracerService):
        settings = Settings(ttl=5)

    return Svc


def _build(cls):
    return cls(ServiceConfig(name="frozen-specs"))


# --- FROZEN ------------------------------------------------------------------


def test_FROZEN_a_declared_spec_cannot_be_mutated_before_any_service_is_built():
    """The import-time write: previously accepted, and every service still got 5."""
    Svc = _declare()

    with pytest.raises(AttributeError, match=REFUSED):
        Svc.settings.ttl = 9

    assert _build(Svc).settings.ttl == 5


def test_FROZEN_a_declared_spec_cannot_be_mutated_after_a_service_is_built():
    Svc = _declare()
    _build(Svc)

    with pytest.raises(AttributeError, match=REFUSED):
        Svc.settings.ttl = 60

    assert Svc.settings.ttl == 5


def test_FROZEN_a_declared_spec_cannot_have_an_attribute_deleted():
    Svc = _declare()

    with pytest.raises(AttributeError, match=REFUSED):
        del Svc.settings.ttl

    assert Svc.settings.ttl == 5


def test_FROZEN_a_spec_passed_to_add_extension_is_frozen_once_bound():
    spec = Settings(ttl=7)
    _build(_declare()).add_extension(spec, "late")

    with pytest.raises(AttributeError, match=REFUSED):
        spec.ttl = 60


def test_CONTROL_a_spec_not_yet_declared_or_bound_is_still_writable():
    """Construction does not freeze: a subclass `__init__` has to set attributes."""
    spec = Settings(ttl=7)

    spec.ttl = 8

    assert spec.ttl == 8


def test_CONTROL_the_bound_runtime_instance_stays_mutable():
    """Runtime state lives on the bound instance, which ADR-0005 isolates rather
    than freezes."""
    Svc = _declare()
    svc = _build(Svc)

    svc.settings.ttl = 60

    assert svc.settings.ttl == 60
    assert svc.settings is not Svc.settings
    assert Svc.settings.ttl == 5


# --- IDEMPOTENT --------------------------------------------------------------


def test_IDEMPOTENT_freezing_a_frozen_spec_is_a_no_op_and_it_stays_frozen():
    spec = Settings()
    spec.freeze()

    spec.freeze()

    with pytest.raises(AttributeError, match=REFUSED):
        spec.ttl = 60


def test_IDEMPOTENT_a_second_instance_of_a_service_class_can_be_built():
    """Both halves at once: the declaration froze the shared spec, and each
    build freezes it again without raising."""
    Svc = _declare()
    first = _build(Svc)
    second = _build(Svc)

    assert first.settings is not second.settings
    assert first.settings.ttl == second.settings.ttl == 5


def test_IDEMPOTENT_one_spec_declared_on_two_classes():
    """`__set_name__` fires once per owning class on the same object."""
    shared = Settings(ttl=3)

    class A(CliffracerService):
        settings = shared

    class B(CliffracerService):
        settings = shared

    assert _build(A).settings.ttl == _build(B).settings.ttl == 3

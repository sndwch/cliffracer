"""An extension argument that is a tuple subclass reaches the extension as that class.

`_safe_clone_arg` and `_snapshot_arg` copied `isinstance(arg, tuple)` item by item into a plain
`tuple(...)`, so a `NamedTuple` given to an extension arrived as a plain tuple, and `limits.calls`
raised `AttributeError` when the service was built. A tuple subclass is now isolated item by item
and rebuilt as its own type, so what is inside it follows the rules for a plain tuple: a
`SharedDependency` is the shared object, a factory is called. A subclass that cannot be rebuilt
from its items is deep-copied whole, with a warning when something inside it was to be shared,
called or built.
"""

import warnings
from typing import NamedTuple

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension, SharedDependency

pytestmark = pytest.mark.unit


class Limits(NamedTuple):
    calls: int
    window: float


class Window(tuple):
    """A tuple subclass that is not a NamedTuple."""


class Router:
    """A shared object that a deep copy would duplicate."""


class Wrapped(NamedTuple):
    router: object
    label: str


class Odd(tuple):
    """A tuple subclass whose constructor takes one value and cannot be built from its items."""

    def __new__(cls, value):
        return super().__new__(cls, (value, value))

    def __reduce__(self):
        return (Odd, (self[0],))


class Uses(Extension):
    def __init__(self, limits) -> None:
        self.limits = limits
        self.calls = limits.calls if hasattr(limits, "calls") else None


def build(extension: Extension) -> Extension:
    class Svc(CliffracerService):
        ext = extension

        def __init__(self) -> None:
            super().__init__(ServiceConfig(name="svc", subject_prefix=None, health_port=0))

    return next(e for e in Svc().container.extensions if isinstance(e, Uses))


def test_a_namedtuple_argument_reaches_the_extension_as_a_namedtuple():
    bound = build(Uses(Limits(10, 1.0)))

    assert type(bound.limits) is Limits
    assert bound.calls == 10
    assert bound.limits == Limits(10, 1.0)


def test_the_frozen_declaration_keeps_the_type_too():
    """A declaration in a class body is frozen when the class is created."""

    class Svc(CliffracerService):
        ext = Uses(Limits(10, 1.0))

    assert type(Svc.__dict__["ext"]._spec_args[0]) is Limits


def test_a_plain_tuple_subclass_keeps_its_type():
    bound = build(Uses(Window((1, 2))))

    assert type(bound.limits) is Window


def test_a_namedtuple_is_not_reported_as_a_copy_that_will_be_refused():
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        build(Uses(Limits(10, 1.0)))


def test_a_shared_dependency_inside_a_namedtuple_is_the_shared_object():
    router = Router()
    declaration = Uses(Wrapped(SharedDependency(router), "a"))

    for bound in (build(declaration), build(declaration)):
        assert type(bound.limits) is Wrapped
        assert bound.limits.router is router
        assert bound.limits.label == "a"


def test_a_shared_dependency_inside_a_namedtuple_survives_the_freeze_of_a_class_body():
    router = Router()

    class Svc(CliffracerService):
        ext = Uses(Wrapped(SharedDependency(router), "a"))

    frozen = Svc.__dict__["ext"]._spec_args[0]
    assert type(frozen) is Wrapped
    assert isinstance(frozen.router, SharedDependency) and frozen.router.value is router


def test_a_factory_inside_a_namedtuple_is_called_for_each_instance():
    made: list[Router] = []

    def make() -> Router:
        made.append(Router())
        return made[-1]

    declaration = Uses(Wrapped(make, "a"))

    first, second = build(declaration).limits, build(declaration).limits

    assert first.router is not second.router
    assert all(type(r) is Router for r in (first.router, second.router))
    assert len(made) == 2


def test_a_tuple_subclass_that_cannot_be_rebuilt_from_its_items_is_copied_whole_and_says_so():
    router = Router()

    with pytest.warns(RuntimeWarning, match="cannot be rebuilt from its items"):
        bound = build(Uses(Odd(SharedDependency(router))))

    assert type(bound.limits) is Odd
    assert bound.limits[0].value is not router


def test_CONTROL_a_tuple_subclass_that_cannot_be_rebuilt_is_not_reported_when_nothing_is_lost():
    """The warning is for a copy that loses sharing, not for every awkward constructor."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        bound = build(Uses(Odd(7)))

    assert type(bound.limits) is Odd and tuple(bound.limits) == (7, 7)


def test_CONTROL_a_shared_dependency_inside_a_plain_tuple_is_the_shared_object():
    router = Router()

    assert build(Uses((SharedDependency(router), 1))).limits[0] is router


def test_CONTROL_a_plain_tuple_is_still_copied_item_by_item():
    inner = [1, 2]
    declaration = Uses((inner, 3))

    first, second = build(declaration).limits, build(declaration).limits

    assert type(first) is tuple
    assert first[0] == inner and first[0] is not inner
    assert first[0] is not second[0]


def test_CONTROL_a_list_and_a_dict_subclass_are_copied_whole_as_before():
    class Pile(list):
        pass

    pile = Pile([1])
    bound = build(Uses(pile)).limits

    assert type(bound) is Pile and bound == pile and bound is not pile

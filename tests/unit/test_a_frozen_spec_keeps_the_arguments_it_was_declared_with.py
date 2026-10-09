"""Changing a declared collection in place cannot reach a service built later.

A specification is the factory for every runtime instance of a service: each
one is rebuilt from the constructor arguments the specification captured. When
an argument is also the object `__init__` keeps (`self.items = items or []`),
the specification's attribute and its captured argument are the SAME list, so
`spec.items.append(...)` on a frozen specification -- which raises nothing,
because freezing guards rebinding and an in-place change is not a rebinding --
rewrote what every later instance was built from. One service's code, or the
caller's own list, then changed what a different service received.

Freezing copies the arguments. These tests change the declared state in place
after a specification is frozen and bind again.

The controls are the other direction: nothing is called while freezing, a
shared dependency stays the one shared object, and an argument that cannot be
copied still fails loudly when an instance is bound.
"""

import threading

import pytest

from cliffracer.core.extension import Extension, ExtensionIsolationError, SharedDependency

pytestmark = pytest.mark.unit


class Holder(Extension):
    """Keeps its declaration arguments as the very objects it was given."""

    def __init__(self, items=None, stats=None, *rest):
        self.items = items if items is not None else []
        self.stats = stats if stats is not None else {}
        self.rest = rest


def test_appending_to_a_frozen_spec_does_not_reach_the_next_instance():
    spec = Holder(items=["initial"])
    spec.freeze()
    before = spec.bind("svc_a", "holder")

    spec.items.append("INJECTED")
    after = spec.bind("svc_b", "holder")

    assert before.items == ["initial"]
    assert after.items == ["initial"], "an in-place change on the spec rewrote a later instance"


def test_a_nested_change_does_not_reach_the_next_instance():
    spec = Holder(stats={"counters": {"write": 0}})
    spec.freeze()

    spec.stats["counters"]["write"] += 1

    assert spec.bind("svc", "holder").stats == {"counters": {"write": 0}}


def test_a_positional_argument_is_copied_too():
    spec = Holder(None, None, ["positional"])
    spec.freeze()

    spec.rest[0].append("INJECTED")

    assert spec.bind("svc", "holder").rest == (["positional"],)


def test_the_callers_own_list_is_not_aliased_after_the_spec_is_frozen():
    declared = ["a"]
    spec = Holder(items=declared)
    spec.freeze()

    declared.append("b")

    assert spec.bind("svc", "holder").items == ["a"]


def test_a_class_declared_spec_is_protected_the_same_way():
    class Declared:
        holder = Holder(items=["initial"])

    Declared.holder.items.append("INJECTED")

    assert Declared.holder.bind("svc", "holder").items == ["initial"]


class Counter:
    """A mutable object that is called with an argument: a value, not a factory."""

    def __init__(self):
        self.seen = ["initial"]

    def __call__(self, item):
        self.seen.append(item)


def test_a_callable_that_takes_arguments_is_copied_like_any_other_value():
    spec = Holder(items=Counter())
    spec.freeze()

    spec.items.seen.append("after-freeze")

    assert spec.bind("svc", "holder").items.seen == ["initial"]


def test_CONTROL_instances_still_get_independent_copies_of_the_declared_state():
    spec = Holder(items=["initial"])
    spec.freeze()
    first, second = spec.bind("a", "holder"), spec.bind("b", "holder")

    first.items.append("first only")

    assert second.items == ["initial"]
    assert first.items == ["initial", "first only"]


def test_CONTROL_freezing_calls_no_factory():
    calls = []

    def factory():
        calls.append("called")
        return ["made"]

    spec = Holder(items=factory)
    spec.freeze()

    assert calls == [], "freezing ran a factory that is meant to run once per bound instance"
    assert spec.bind("svc", "holder").items == ["made"]
    assert calls == ["called"]


def test_CONTROL_a_shared_dependency_stays_the_one_shared_object():
    shared = {"counter": 0}
    spec = Holder(stats=SharedDependency(shared))
    spec.freeze()

    assert spec.bind("a", "holder").stats is shared
    assert spec.bind("b", "holder").stats is shared


def test_CONTROL_an_argument_that_cannot_be_copied_still_fails_when_bound():
    spec = Holder(items=threading.Lock())

    spec.freeze()

    with pytest.raises(ExtensionIsolationError):
        spec.bind("svc", "holder")

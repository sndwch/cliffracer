"""A `SharedDependency` inside a list or a dict is shared, as it is at the top level and in a tuple.

`_safe_clone_arg` unwrapped a `SharedDependency` at the top level and recursed into tuples only.
A list or a dict went to `deepcopy` whole, so a wrapper inside one was never unwrapped: with an
uncopyable payload the error named the type `list` and told the caller to wrap with
`SharedDependency`, which they had; with a copyable one nothing was raised, the extension received
a `SharedDependency` object, and its `.value` was a private copy, so the sharing the wrapper asks
for was silently lost. The snapshot taken when a specification is frozen did the same copy, one
step earlier.

A zero-argument callable is called per bound instance wherever it sits, so what a callable that
is a callback and not a factory becomes is stated here too.
"""

from __future__ import annotations

import threading

import pytest

from cliffracer.core.extension import (
    Extension,
    ExtensionIsolationError,
    SharedDependency,
    _safe_clone_arg,
)

pytestmark = pytest.mark.unit


class Routers(Extension):
    def __init__(self, routers=None, by_name=None) -> None:
        self.routers = routers
        self.by_name = by_name


def test_a_list_item_wrapped_in_shared_dependency_is_the_one_object():
    lock = threading.Lock()

    cloned = _safe_clone_arg([SharedDependency(lock)])

    assert isinstance(cloned, list)
    assert cloned[0] is lock


def test_a_dict_value_wrapped_in_shared_dependency_is_the_one_object():
    lock = threading.Lock()

    cloned = _safe_clone_arg({"k": SharedDependency(lock)})

    assert isinstance(cloned, dict)
    assert cloned["k"] is lock


def test_a_copyable_payload_is_shared_not_copied_and_not_left_wrapped():
    """The silent form: no error, the extension got the wrapper, and `.value` was a copy."""
    connections = {"conns": 1}

    cloned = _safe_clone_arg([SharedDependency(connections)])

    assert cloned[0] is connections
    assert not isinstance(cloned[0], SharedDependency)


def test_the_wrapper_is_found_through_nested_containers():
    lock = threading.Lock()

    cloned = _safe_clone_arg({"outer": [({"inner": SharedDependency(lock)},)]})

    assert cloned["outer"][0][0]["inner"] is lock


def test_the_other_items_of_the_list_are_still_isolated():
    plain = {"a": [1, 2]}
    lock = threading.Lock()
    source = [plain, SharedDependency(lock), "text", 3]

    cloned = _safe_clone_arg(source)

    assert cloned[0] == plain and cloned[0] is not plain
    assert cloned[0]["a"] is not plain["a"]
    assert cloned[1] is lock
    assert cloned[2:] == ["text", 3]
    assert cloned is not source
    assert source[1].value is lock, "the declaration's own list is untouched"


def test_an_uncopyable_item_in_a_list_is_named_not_the_list():
    with pytest.raises(ExtensionIsolationError) as caught:
        _safe_clone_arg([threading.Lock()])

    assert "'lock'" in str(caught.value) and "'list'" not in str(caught.value)


def test_dict_keys_are_left_alone():
    key = ("k", 1)

    cloned = _safe_clone_arg({key: SharedDependency(threading.Lock())})

    assert next(iter(cloned)) is key


def test_a_list_that_holds_one_object_twice_still_holds_one_copy_of_it():
    """Isolation keeps the aliasing `deepcopy` keeps within one argument."""
    shared_inside = {"n": 1}

    cloned = _safe_clone_arg([shared_inside, shared_inside])

    assert cloned[0] is cloned[1]
    assert cloned[0] is not shared_inside


def test_an_object_that_is_not_a_container_keeps_its_aliasing_too():
    """The deep copies share the memo, so one object held twice is one copy, not two."""
    held = {1, 2}

    cloned = _safe_clone_arg([held, {"again": held}])

    assert cloned[0] is cloned[1]["again"]
    assert cloned[0] is not held


def test_a_list_that_contains_itself_is_cloned_rather_than_recursed_into_for_ever():
    cyclic: list = [1]
    cyclic.append(cyclic)

    cloned = _safe_clone_arg(cyclic)

    assert cloned[0] == 1
    assert cloned[1] is cloned


def test_a_subclass_of_list_or_dict_is_deep_copied_whole_as_before():
    from collections import OrderedDict, UserList

    class Routes(list):
        pass

    ordered = OrderedDict(a=SharedDependency({"x": 1}))
    user = UserList([SharedDependency({"x": 1})])
    routes = Routes([SharedDependency({"x": 1})])

    cloned_ordered = _safe_clone_arg(ordered)
    cloned_user = _safe_clone_arg(user)

    assert isinstance(cloned_ordered, OrderedDict)
    assert isinstance(cloned_ordered["a"], SharedDependency)
    assert isinstance(cloned_user, UserList)
    assert isinstance(cloned_user[0], SharedDependency)
    cloned_routes = _safe_clone_arg(routes)
    assert type(cloned_routes) is Routes
    assert isinstance(cloned_routes[0], SharedDependency)


def test_a_bound_extension_receives_the_shared_object_from_a_list_and_a_dict():
    router = object()
    spec = Routers(routers=[SharedDependency(router)], by_name={"main": SharedDependency(router)})

    bound = spec.bind(None, "routers")

    assert bound.routers[0] is router
    assert bound.by_name["main"] is router


def test_freezing_the_spec_does_not_copy_what_a_wrapper_inside_a_list_shares():
    """The snapshot taken at freeze is where the copy used to happen first."""
    router = {"conns": 0}
    spec = Routers(routers=[SharedDependency(router)], by_name={"m": SharedDependency(router)})

    spec.freeze()

    assert spec._spec_kwargs["routers"][0].value is router
    assert spec._spec_kwargs["by_name"]["m"].value is router
    assert spec.bind(None, "r").routers[0] is router


def test_freezing_still_copies_a_plain_list_so_the_callers_list_is_not_aliased():
    declared = [{"a": 1}]
    spec = Routers(routers=declared)

    spec.freeze()
    declared[0]["a"] = 2
    declared.append({"b": 1})

    assert spec.bind(None, "r").routers == [{"a": 1}]


# What a callable that is a callback, and not a factory, becomes.


def test_a_zero_argument_callable_is_called_once_per_instance_and_its_product_used():
    calls: list[int] = []

    def callback():
        calls.append(1)

    assert _safe_clone_arg(callback) is None
    assert calls == [1]


def test_a_callback_that_must_arrive_as_a_callable_is_wrapped_in_shared_dependency():
    calls: list[int] = []

    def callback():
        calls.append(1)

    cloned = _safe_clone_arg(SharedDependency(callback))

    assert cloned is callback
    assert calls == []


def test_the_same_holds_inside_a_list():
    calls: list[int] = []

    def callback():
        calls.append(1)
        return "product"

    assert _safe_clone_arg([callback]) == ["product"]
    assert _safe_clone_arg([SharedDependency(callback)]) == [callback]
    assert calls == [1]

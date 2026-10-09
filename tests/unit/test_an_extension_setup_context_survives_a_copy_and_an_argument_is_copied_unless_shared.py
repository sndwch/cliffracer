"""`ExtensionSetupContext` can be copied, and an extension's arguments are isolated as documented.

Its `__getattr__` delegated every name to `self.service`, including `service` itself on an
instance whose fields were not restored yet, which is what `copy` and `pickle` build first:
the lookup re-entered `__getattr__` for ever, so `copy.deepcopy(ctx)` raised `RecursionError`
and, from an extension's constructor argument, surfaced as a confusing `ExtensionIsolationError`.
Protocol names (`__setstate__`, `__deepcopy__`) belong to the context, not the service behind it.

`create_instance`'s docstring said a non-collection argument was shared by reference. It is
deep-copied, an argument that cannot be copied is refused, and `SharedDependency` shares; those
three are pinned here.
"""

import copy
import threading

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.extension import (
    Extension,
    ExtensionIsolationError,
    ExtensionSetupContext,
    SharedDependency,
)

pytestmark = pytest.mark.unit


class Service:
    answer = 42


def _ctx(service=None) -> ExtensionSetupContext:
    return ExtensionSetupContext(
        service_config=ServiceConfig(name="ctx"),
        broker_url="nats://x",
        service=service or Service(),
    )


def test_a_context_delegates_a_name_it_does_not_have_to_the_service():
    assert _ctx().answer == 42


def test_an_uninitialised_context_raises_attribute_error_not_recursion():
    bare = ExtensionSetupContext.__new__(ExtensionSetupContext)

    with pytest.raises(AttributeError):
        bare.answer  # noqa: B018 - the lookup is the thing under test
    with pytest.raises(AttributeError):
        bare.service  # noqa: B018


def test_a_context_can_be_shallow_copied():
    ctx = _ctx()

    again = copy.copy(ctx)

    assert again.service is ctx.service
    assert again.broker_url == "nats://x"


def test_a_context_can_be_deep_copied():
    ctx = _ctx()

    again = copy.deepcopy(ctx)

    assert again.broker_url == "nats://x"
    assert again.answer == 42


def test_a_protocol_name_is_not_delegated_to_a_permissive_service():
    class Permissive:
        def __getattr__(self, name):
            return "anything"

    ctx = _ctx(Permissive())

    assert ctx.some_name == "anything"
    assert not hasattr(ctx, "__setstate__")


class Holder(Extension):
    def __init__(self, client) -> None:
        self.client = client


class Client:
    pass


def test_an_argument_object_is_deep_copied_not_shared():
    client = Client()

    bound = Holder(client).create_instance(None, "holder")

    assert bound.client is not client
    assert isinstance(bound.client, Client)


def test_an_argument_that_cannot_be_copied_is_refused():
    with pytest.raises(ExtensionIsolationError):
        Holder(threading.Lock()).create_instance(None, "holder")


def test_a_shared_dependency_is_passed_by_reference():
    client = Client()

    bound = Holder(SharedDependency(client)).create_instance(None, "holder")

    assert bound.client is client

"""An extension argument that is copied but is not plain data warns, ahead of the copy being refused.

An extension's arguments are deep-copied for each bound service. An object that must be one per process
(a client, a store, a limiter) is cloned if it happens to be copyable, and which side of that line it
falls on can depend on its lifecycle: an unconnected `nats.NATS()` copies silently where a connected
one is refused. The rule is to become an allow-list, plain data copied and anything else refused unless
it is a `SharedDependency` or a factory, after one release that warns. This is that release: what is
outside the list is still copied, with a `FutureWarning` that names the extension, the argument and what
to write instead.
"""

import dataclasses
import datetime
import decimal
import enum
import pathlib
import threading
import uuid
import warnings

import nats
import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import (
    Extension,
    ExtensionIsolationError,
    SharedDependency,
)

pytestmark = pytest.mark.unit


class Holder(Extension):
    def __init__(self, thing=None) -> None:
        self.thing = thing


class Settings:
    """An ordinary object: not plain data."""

    def __init__(self) -> None:
        self.ttl = 5


class Colour(enum.Enum):
    RED = 1


class Model(BaseModel):
    ttl: int = 5


@dataclasses.dataclass
class Record:
    ttl: int = 5


def _bind(extension: Extension):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bound = extension.create_instance(None, "x")
    return bound, [w for w in caught if issubclass(w.category, FutureWarning)]


def test_an_unconnected_nats_client_is_copied_with_a_warning_that_says_what_to_do():
    original = nats.NATS()

    bound, warned = _bind(Holder(original))

    assert bound.thing is not original, "still copied: the behaviour is unchanged for this release"
    (warning,) = warned
    message = str(warning.message)
    assert "Holder argument 1" in message, message
    assert "nats.aio.client.Client" in message, message
    assert "future release" in message and "refuse" in message, message
    assert "SharedDependency(...)" in message and "zero-argument callable" in message, message


def test_an_ordinary_object_warns_naming_the_keyword():
    _, warned = _bind(Holder(thing=Settings()))

    (warning,) = warned
    assert "Holder(thing=...)" in str(warning.message)
    assert "Settings" in str(warning.message)


def test_a_declaration_built_into_two_services_warns_once_under_the_default_filter():
    """Python's `default` filter shows a warning once per location and text, so a process that builds
    many services from one declaration is told once."""

    class Svc(CliffracerService):
        holder = Holder(Settings())

    with warnings.catch_warnings(record=True) as caught:
        warnings.resetwarnings()
        warnings.simplefilter("default")
        Svc(ServiceConfig(name="one", health_port=0))
        Svc(ServiceConfig(name="two", health_port=0))

    assert len([w for w in caught if issubclass(w.category, FutureWarning)]) == 1


@pytest.mark.parametrize(
    "plain",
    [
        "text",
        b"bytes",
        3,
        2.5,
        True,
        None,
        [1, 2],
        {"a": 1},
        {1, 2},
        frozenset({1}),
        (1, 2),
        Model(),
        Record(),
        datetime.datetime(2026, 1, 1),
        datetime.date(2026, 1, 1),
        datetime.timedelta(seconds=1),
        decimal.Decimal("1.5"),
        uuid.UUID(int=1),
        pathlib.PurePosixPath("/a"),
        range(3),
        Colour.RED,
    ],
    ids=lambda v: type(v).__name__,
)
def test_plain_data_is_copied_without_a_word(plain):
    _, warned = _bind(Holder(plain))

    assert warned == []


def test_an_object_nested_in_a_list_or_a_dict_warns_too():
    _, in_list = _bind(Holder([Settings()]))
    _, in_dict = _bind(Holder({"k": Settings()}))

    assert len(in_list) == 1 and len(in_dict) == 1


def test_a_shared_dependency_a_factory_and_a_nested_extension_say_nothing():
    for given in (SharedDependency(Settings()), Settings, Holder()):
        _, warned = _bind(Holder(given))
        assert warned == [], given


def test_something_a_copy_returns_as_it_is_was_never_cloned_and_says_nothing():
    for given in (len, Settings, Colour, ValueError):
        _, warned = _bind(Holder(given))
        assert warned == [], given


def test_an_object_that_cannot_be_copied_is_still_refused_and_does_not_warn_first():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ExtensionIsolationError):
            Holder(threading.Lock()).create_instance(None, "x")

    assert [w for w in caught if issubclass(w.category, FutureWarning)] == []


def test_the_copy_equals_the_original_so_nothing_a_service_relied_on_changes():
    original = Settings()

    bound, _ = _bind(Holder(original))

    assert bound.thing is not original and bound.thing.ttl == original.ttl == 5

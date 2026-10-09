"""`wrap_exception` and `ErrorHandler` annotate an exception without losing it.

Two promises are held here. Each wrapped exception records its own cause and
leaves the caller's details alone, however many times one details mapping is
reused. And a class that cannot be built from a message and details is refused
by name, up front, rather than failing with a constructor `TypeError` while the
real failure is being handled, or being built with its fields swapped.
"""

import inspect

import pytest

from cliffracer.core import exceptions
from cliffracer.core.exceptions import (
    ClientOutOfDateError,
    CliffracerError,
    ConfigurationError,
    ErrorHandler,
    RpcRefusedError,
    RpcValidationError,
    ServiceError,
    wrap_exception,
)

pytestmark = pytest.mark.unit

CANNOT_WRAP = (RpcRefusedError, ClientOutOfDateError, RpcValidationError)


def test_wrapping_leaves_the_callers_details_alone():
    details = {"request_id": "r1"}

    wrap_exception(ValueError("first"), ServiceError, "op", details)

    assert details == {"request_id": "r1"}


def test_the_callers_details_are_kept_beside_the_original_exception():
    wrapped = wrap_exception(ValueError("first"), ServiceError, "op", {"request_id": "r1"})

    assert wrapped.details["request_id"] == "r1"
    assert wrapped.details["original_exception"]["message"] == "first"
    assert wrapped.details["original_exception"]["type"] == "ValueError"


def test_two_wraps_of_one_details_mapping_each_report_their_own_cause():
    shared = {"request_id": "r1"}

    first = wrap_exception(ValueError("first"), ServiceError, "op1", shared)
    second = wrap_exception(KeyError("second"), ServiceError, "op2", shared)

    assert first.details["original_exception"]["message"] == "first"
    assert second.details["original_exception"]["type"] == "KeyError"
    assert first.details is not second.details


def test_a_wrapped_exception_chains_to_the_original():
    original = ValueError("boom")

    wrapped = wrap_exception(original, ServiceError, "saving")

    assert wrapped.__cause__ is original
    assert wrapped.message == "saving"


def test_an_error_handler_used_twice_reports_each_failure_as_its_own():
    handler = ErrorHandler("saving order", details={"order": "o-1"})
    caught: list[CliffracerError] = []

    for original in (ValueError("first"), KeyError("second")):
        with pytest.raises(ServiceError) as raised:
            with handler:
                raise original
        caught.append(raised.value)

    assert [c.details["original_exception"]["type"] for c in caught] == ["ValueError", "KeyError"]
    assert handler.details == {"order": "o-1"}


async def test_an_async_error_handler_used_twice_reports_each_failure_as_its_own():
    handler = ErrorHandler("saving order")
    caught: list[CliffracerError] = []

    for original in (ValueError("first"), KeyError("second")):
        with pytest.raises(ServiceError) as raised:
            async with handler:
                raise original
        caught.append(raised.value)

    assert [c.details["original_exception"]["type"] for c in caught] == ["ValueError", "KeyError"]


@pytest.mark.parametrize("cls", CANNOT_WRAP, ids=lambda c: c.__name__)
def test_a_class_that_cannot_take_a_message_and_details_is_refused_by_name(cls):
    with pytest.raises(ConfigurationError) as raised:
        wrap_exception(ValueError("boom"), cls, "op")

    assert cls.__name__ in str(raised.value)


@pytest.mark.parametrize("cls", CANNOT_WRAP, ids=lambda c: c.__name__)
def test_an_error_handler_refuses_such_a_class_when_it_is_built(cls):
    with pytest.raises(ConfigurationError) as raised:
        ErrorHandler("saving order", exception_class=cls)

    assert cls.__name__ in str(raised.value)


def _classes() -> list[type[CliffracerError]]:
    """Each exception class the module defines, once, whatever aliases name it."""
    unique = {
        obj: None
        for _, obj in inspect.getmembers(exceptions, inspect.isclass)
        if issubclass(obj, CliffracerError) and obj.__module__ == exceptions.__name__
    }
    return sorted(unique, key=lambda c: c.__name__)


def _builds_from_message_and_details(cls: type[CliffracerError]) -> bool:
    try:
        built = cls("the message", {"key": "value"})  # type: ignore[call-arg]
    except TypeError:
        return False
    return built.message == "the message" and built.details == {"key": "value"}


@pytest.mark.parametrize("cls", _classes(), ids=lambda c: c.__name__)
def test_every_exception_class_is_wrapped_or_refused_as_it_actually_builds(cls):
    """The refusal follows what the constructor does, so a class added later with
    its own signature is judged by that, not by a list that can drift."""
    if _builds_from_message_and_details(cls):
        wrapped = wrap_exception(ValueError("boom"), cls, "op")
        assert isinstance(wrapped, cls)
        assert wrapped.message == "op"
    else:
        with pytest.raises(ConfigurationError):
            wrap_exception(ValueError("boom"), cls, "op")


def test_CONTROL_the_class_census_sees_both_kinds():
    """Without both kinds in the census, the test above could pass by judging nothing."""
    built = [c for c in _classes() if _builds_from_message_and_details(c)]
    refused = [c for c in _classes() if not _builds_from_message_and_details(c)]

    assert len(built) >= 10, built
    assert set(refused) >= set(CANNOT_WRAP), refused

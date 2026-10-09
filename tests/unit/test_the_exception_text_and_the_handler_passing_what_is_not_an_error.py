"""What an exception prints, and what an `ErrorHandler` leaves alone.

The module's other behaviour (pickling, `wrap_exception`, the handler's wrapping and suppression)
has its own tests. These pin two things that had none: the text of `CliffracerError` with and
without details, and that a `BaseException` that is not an `Exception` (cancellation, an
interrupt) passes through an `ErrorHandler` unwrapped instead of being turned into a service
error that a surrounding task would then not recognise as cancellation.
"""

import asyncio

import pytest

from cliffracer.core.exceptions import CliffracerError, ErrorHandler, ServiceError

pytestmark = pytest.mark.unit


def test_the_text_is_the_message_when_there_are_no_details():
    assert str(CliffracerError("it broke")) == "it broke"


def test_the_text_appends_the_details_when_there_are_some():
    assert str(CliffracerError("it broke", {"key": "value"})) == (
        "it broke - Details: {'key': 'value'}"
    )
    assert str(CliffracerError("it broke", ["a", "b"])) == "it broke - Details: ['a', 'b']"


def test_empty_details_print_as_none_were_given():
    assert str(CliffracerError("it broke", {})) == "it broke"
    assert str(CliffracerError("it broke", [])) == "it broke"


def test_two_errors_built_without_details_do_not_share_a_mapping():
    first, second = CliffracerError("one"), CliffracerError("two")
    first.details["added"] = True  # type: ignore[index]

    assert second.details == {}


def test_a_cancellation_passes_through_a_sync_handler_unwrapped():
    with pytest.raises(asyncio.CancelledError):
        with ErrorHandler("operation failed", ServiceError):
            raise asyncio.CancelledError


async def test_a_cancellation_passes_through_an_async_handler_unwrapped():
    with pytest.raises(asyncio.CancelledError):
        async with ErrorHandler("operation failed", ServiceError):
            raise asyncio.CancelledError


def test_an_interrupt_is_not_wrapped_either_even_when_the_handler_suppresses():
    with pytest.raises(KeyboardInterrupt):
        with ErrorHandler("operation failed", ServiceError, reraise=False):
            raise KeyboardInterrupt


def test_CONTROL_an_ordinary_failure_is_wrapped():
    with pytest.raises(ServiceError, match="operation failed"):
        with ErrorHandler("operation failed", ServiceError):
            raise RuntimeError("underlying")

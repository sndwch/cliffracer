"""An exception crosses a process boundary whole, and an `ErrorHandler` that
suppresses a failure says so.

`pickle` rebuilds an exception by calling `cls(*args)` and then restoring its
`__dict__`. Most classes here take `(message, details)`, but
`ClientOutOfDateError(service, changed, missing)` and `RpcRefusedError(reason)`
build their message from their own arguments and `RpcValidationError` takes
`(details, message)`. So unpickling `ClientOutOfDateError` raised a `TypeError`,
and the other two came back with the wrong `args` (a doubled `refused: ` prefix;
the default message): their attributes were restored, but `repr()` and anything
that re-raises from `args` saw the wrong text.

`ErrorHandler(reraise=False)` returned `True` from `__exit__` and left no trace:
the wrapped exception it built was dropped. It now logs the failure it
suppressed. And `wrap_exception` no longer copies the original's `args` into
`details`, which `str()` renders: a connection string in an exception's
arguments appeared three times in a log line.
"""

import inspect
import pickle

import pytest
from loguru import logger

from cliffracer.core import exceptions
from cliffracer.core.exceptions import (
    ClientOutOfDateError,
    CliffracerError,
    ErrorHandler,
    RpcBusyError,
    RpcDeadlineExceededError,
    RpcRefusedError,
    RpcStreamGapError,
    RpcValidationError,
    ServiceError,
    wrap_exception,
)

pytestmark = pytest.mark.unit

DETAILS = [{"loc": ["amount"], "msg": "not a number"}]

# One realistic instance of each class that does not build as (message, details).
# Every other class is built as `cls("the message", {"key": "value"})`.
SPECIAL = {
    ClientOutOfDateError: lambda: ClientOutOfDateError("orders", ["create"], ["cancel"]),
    RpcRefusedError: lambda: RpcRefusedError("quota exceeded"),
    RpcDeadlineExceededError: lambda: RpcDeadlineExceededError(
        "orders.rpc.stuck: cut off", budget=0.05, elapsed=0.051, set_by="caller"
    ),
    RpcBusyError: lambda: RpcBusyError("orders.rpc.report: not admitted", limit=32, in_flight=32),
    RpcValidationError: lambda: RpcValidationError(DETAILS, "refused before sending"),
    RpcStreamGapError: lambda: RpcStreamGapError("logs.rpc.tail", expected=2, got=3, items=2),
}


def _classes() -> list[type[CliffracerError]]:
    unique = {
        obj: None
        for _, obj in inspect.getmembers(exceptions, inspect.isclass)
        if issubclass(obj, CliffracerError) and obj.__module__ == exceptions.__name__
    }
    return sorted(unique, key=lambda c: c.__name__)


def _instance(cls: type[CliffracerError]) -> CliffracerError:
    if cls in SPECIAL:
        return SPECIAL[cls]()
    return cls("the message", {"key": "value"})


def _round_trip(error: BaseException) -> BaseException:
    return pickle.loads(pickle.dumps(error))


@pytest.mark.parametrize("cls", _classes(), ids=lambda c: c.__name__)
def test_every_exception_class_survives_a_pickle_round_trip(cls):
    error = _instance(cls)

    again = _round_trip(error)

    assert type(again) is cls
    assert again.args == error.args
    assert again.__dict__ == error.__dict__
    assert str(again) == str(error)


def test_CONTROL_the_census_includes_the_classes_that_do_not_build_as_message_and_details():
    """Without these in the census the round trip above could pass by judging
    only the easy classes, and a new oddly-built class would be built wrongly."""
    assert set(SPECIAL) <= set(_classes())
    assert len(_classes()) >= 15


def test_a_refused_error_keeps_its_reason_and_its_prefix():
    again = _round_trip(RpcRefusedError("quota exceeded"))

    assert again.reason == "quota exceeded"
    assert str(again) == "refused: quota exceeded"
    assert again.args == ("refused: quota exceeded",), again.args


def test_an_out_of_date_error_keeps_the_methods_it_names():
    again = _round_trip(ClientOutOfDateError("orders", ["create"], ["cancel"]))

    assert (again.service, again.changed, again.missing) == ("orders", ["create"], ["cancel"])


def test_a_validation_error_keeps_its_details_as_a_list():
    again = _round_trip(RpcValidationError(DETAILS, "refused before sending"))

    assert again.details == DETAILS
    assert again.message == "refused before sending"
    assert again.args == ("refused before sending",), again.args


def test_a_rebuilt_error_is_still_raised_and_caught_as_its_own_class():
    with pytest.raises(ServiceError) as raised:
        raise _round_trip(exceptions.ConfigurationError("bad", {"field": "name"}))

    assert isinstance(raised.value, exceptions.ConfigurationError)
    assert raised.value.details == {"field": "name"}


class _Sink:
    """What loguru wrote, as plain text, at WARNING and above."""

    def __init__(self):
        self.lines: list[str] = []
        self._id = logger.add(self.lines.append, level="WARNING", format="{message}")

    def close(self):
        logger.remove(self._id)


@pytest.fixture
def sink():
    s = _Sink()
    try:
        yield s
    finally:
        s.close()


def test_a_suppressed_failure_is_logged_with_its_type_and_message(sink):
    with ErrorHandler("saving order", reraise=False):
        raise ValueError("disk full")

    assert len(sink.lines) == 1, sink.lines
    line = sink.lines[0]
    assert "saving order" in line
    assert "ValueError" in line
    assert "disk full" in line


async def test_an_async_suppressed_failure_is_logged_too(sink):
    async with ErrorHandler("saving order", reraise=False):
        raise KeyError("o-1")

    assert len(sink.lines) == 1, sink.lines
    assert "saving order" in sink.lines[0]
    assert "KeyError" in sink.lines[0]


def test_CONTROL_a_failure_that_is_raised_is_not_also_logged(sink):
    with pytest.raises(ServiceError):
        with ErrorHandler("saving order"):
            raise ValueError("disk full")

    assert sink.lines == []


def test_CONTROL_a_cliffracer_error_passes_through_unlogged_and_unwrapped(sink):
    original = exceptions.ConfigurationError("bad")

    with pytest.raises(exceptions.ConfigurationError) as raised:
        with ErrorHandler("saving order", reraise=False):
            raise original

    assert raised.value is original
    assert sink.lines == []


def test_the_original_text_appears_once_in_a_wrapped_exception_with_its_own_message():
    dsn = "postgres://app:hunter2@db/orders"

    wrapped = wrap_exception(ConnectionError(dsn), ServiceError, "saving order")

    assert str(wrapped).count("hunter2") == 1, str(wrapped)
    assert "args" not in wrapped.details["original_exception"]


def test_the_original_keeps_its_arguments_as_the_cause():
    original = ConnectionError(111, "refused")

    wrapped = wrap_exception(original, ServiceError, "saving order")

    assert wrapped.__cause__ is original
    assert wrapped.__cause__.args == (111, "refused")

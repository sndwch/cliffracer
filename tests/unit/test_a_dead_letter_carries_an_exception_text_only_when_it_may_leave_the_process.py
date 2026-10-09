"""A dead letter's `error` is the exception's type unless `expose_internal_errors` lets its text out.

`expose_internal_errors` is "whether an exception's own text may leave the process", and it governs
the wire, the describe reply, the health endpoint and the error written into a distributed cron
record. The dead-letter stream leaves the process too: anyone who can read it, `cliffracer-dlq`
included, could read the text of a handler's exception, a DSN or a password in it, whichever value
the flag had. The record carries the exception's type when the flag is off.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.dispatch.dlq import DeadLetterPublisher
from cliffracer.core.extension import RejectMessage
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

SECRET = "SECRET-DB-PASSWORD-hunter2 in the connection string"


def _publisher(*, expose: bool) -> tuple[DeadLetterPublisher, AsyncMock]:
    config = ServiceConfig(
        name="orders",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
        expose_internal_errors=expose,
    )
    js = AsyncMock()
    conn = SimpleNamespace(jetstream_active=True, js=js, nc=AsyncMock())
    return DeadLetterPublisher(config, lambda: conn), js


def _delivery() -> SimpleNamespace:
    metadata = SimpleNamespace(
        num_delivered=3,
        stream="EVENTS",
        consumer="orders-durable",
        sequence=SimpleNamespace(stream=42, consumer=7),
    )
    return SimpleNamespace(
        subject="events.order",
        data=b'{"number": 1}',
        headers={"Content-Type": "application/json"},
        metadata=metadata,
    )


async def _error_of(error, *, expose: bool) -> str:
    publisher, js = _publisher(expose=expose)
    await publisher.dead_letter_terminated(_delivery(), error, 3)
    (call,) = js.publish.await_args_list
    return json.loads(call.args[1])["error"]


async def test_a_handlers_exception_text_is_withheld_when_the_flag_is_off():
    error = await _error_of(RuntimeError(SECRET), expose=False)

    assert error == "RuntimeError"
    assert SECRET not in error


async def test_a_handlers_exception_text_is_written_when_the_flag_is_on():
    error = await _error_of(RuntimeError(SECRET), expose=True)

    assert error == SECRET


@pytest.mark.parametrize("expose", [False, True])
async def test_the_type_is_the_exceptions_own_class(expose):
    class DatabaseUnavailable(Exception):
        pass

    error = await _error_of(DatabaseUnavailable(SECRET), expose=expose)

    assert error == (SECRET if expose else "DatabaseUnavailable")


async def test_the_reason_of_a_gate_that_crashed_is_the_frameworks_own_text_and_is_kept():
    crash = RejectMessage("extension gate failed: internal error", hook_crash=True)

    error = await _error_of(crash, expose=False)

    assert error == "extension gate failed: internal error"


async def test_a_reason_that_is_not_an_exception_is_written_as_given():
    error = await _error_of("delivery limit reached", expose=False)

    assert error == "delivery limit reached"


async def test_the_record_does_not_carry_the_text_anywhere_else_when_the_flag_is_off():
    publisher, js = _publisher(expose=False)

    await publisher.dead_letter_terminated(_delivery(), RuntimeError(SECRET), 3)

    (call,) = js.publish.await_args_list
    assert SECRET not in call.args[1].decode()
    assert SECRET not in json.dumps({k: str(v) for k, v in call.kwargs.items()})


async def test_CONTROL_a_decode_failure_stays_readable_with_the_flag_off():
    publisher, js = _publisher(expose=False)

    await publisher.dead_letter_decode_error(_delivery(), ValueError("Expecting value"))

    (call,) = js.publish.await_args_list
    assert json.loads(call.args[1])["error"].startswith("Decode error:")


async def test_the_frameworks_own_sentences_are_kept_the_overrun_and_the_missing_package():
    """Marked `own_text`: nothing the application put in them, and the operator needs them."""
    from unittest.mock import patch

    from cliffracer.core import validation
    from cliffracer.core.error_text import has_own_text

    with patch.object(validation, "msgpack", None):
        for call in (lambda: validation.pack_msgpack({}), lambda: validation.unpack_msgpack(b"")):
            with pytest.raises(ImportError) as missing:
                call()
            assert has_own_text(missing.value)
            assert await _error_of(missing.value, expose=False) == str(missing.value)


async def test_CONTROL_a_handlers_own_import_error_is_withheld_like_any_other():
    error = await _error_of(ImportError(f"No module named 'x' at /srv/{SECRET}"), expose=False)

    assert error == "ImportError"


class Flagged(Exception):
    """An application exception that happens to carry a truthy `hook_crash` attribute."""

    hook_crash = True


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(RejectMessage(SECRET), "RejectMessage", id="an-extensions-own-rejection"),
        pytest.param(
            Flagged(SECRET), "Flagged", id="an-application-error-with-a-hook-crash-attribute"
        ),
    ],
)
async def test_only_a_crashed_gates_reason_keeps_its_text(error, expected):
    """With the flag off, the text kept is the framework's own: a crashed gate's RejectMessage.
    An extension's own rejection, and an application error that merely carries the attribute,
    are written as their type."""
    assert await _error_of(error, expose=False) == expected

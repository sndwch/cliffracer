"""The id a dispatch ran under travels to the transport on the exception, and only as a usable string.

`handle_event` re-raises to the layer that decides the message's fate, which may dead-letter it after
the dispatch's context is gone, so the id is recorded on the exception. What is read back is a
non-empty string or `None`: an empty id, or something that is not text, is "no id", and the transport
mints one.

A `fails_closed` hook that crashes surfaces as a `RejectMessage` marked `hook_crash`. A caller that
does not ask for errors gets it logged and an `OK` outcome; one that does ask gets the exception,
carrying the id the message was dispatched under.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cliffracer.core.dispatch import (
    DeadLetterPublisher,
    DispatchOutcome,
    EventDispatcher,
    ExtensionPipeline,
)
from cliffracer.core.dispatch.events import carried_correlation_id, carry_correlation_id
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit

WIRE_ID = "corr_fromthewire000001"


def test_an_id_that_was_carried_is_read_back():
    error = RuntimeError("boom")

    carry_correlation_id(error, "corr_abc")

    assert carried_correlation_id(error) == "corr_abc"


def test_no_id_is_read_back_as_none_after_carrying_an_empty_one():
    error = RuntimeError("boom")

    carry_correlation_id(error, "")

    assert carried_correlation_id(error) is None


@pytest.mark.parametrize("not_text", [123, ["corr_abc"], b"corr_abc"], ids=["int", "list", "bytes"])
def test_a_value_on_the_exception_that_is_not_a_string_is_no_id(not_text):
    error = RuntimeError("boom")
    carry_correlation_id(error, "corr_abc")
    assert carried_correlation_id(error) == "corr_abc", "fixture: the attribute is the one read"
    error.__dict__[next(iter(error.__dict__))] = not_text

    assert carried_correlation_id(error) is None


class Crashes(Extension):
    """What a `fails_closed` hook does when its backend is unreachable."""

    fails_closed = True

    async def worker_setup(self, ctx):
        raise RuntimeError("issuer unreachable")


class Refuses(Extension):
    fails_closed = True

    async def worker_setup(self, ctx):
        raise RejectMessage("unauthenticated")


def _dispatcher(extension: Extension) -> tuple[EventDispatcher, list[int]]:
    seen: list[int] = []

    def handler(n: int) -> None:
        seen.append(n)

    registry = ServiceRegistry()
    registry.event_handlers["evt.a"] = handler
    config = ServiceConfig(name="events_svc", health_port=0)
    dispatcher = EventDispatcher(
        registry,
        config,
        ExtensionPipeline([extension]),
        MagicMock(spec=DeadLetterPublisher),
    )
    return dispatcher, seen


def _message() -> SimpleNamespace:
    return SimpleNamespace(subject="evt.a", data=b'{"n": 1}', headers={"X-Correlation-ID": WIRE_ID})


async def test_a_crashed_hook_is_logged_not_raised_for_a_caller_that_does_not_ask_for_errors():
    dispatcher, seen = _dispatcher(Crashes())

    outcome = await dispatcher.handle_event(_message(), pattern="evt.a", raise_on_error=False)

    assert outcome == DispatchOutcome.OK
    assert seen == [], "a crashed fails_closed hook must still stop the handler"


async def test_a_crashed_hook_is_raised_with_the_id_of_its_dispatch_for_a_caller_that_asks():
    dispatcher, seen = _dispatcher(Crashes())

    with pytest.raises(RejectMessage) as raised:
        await dispatcher.handle_event(_message(), pattern="evt.a", raise_on_error=True)

    assert raised.value.hook_crash is True
    assert carried_correlation_id(raised.value) == WIRE_ID
    assert seen == []


async def test_CONTROL_an_authored_refusal_is_not_raised_even_for_a_caller_that_asks():
    """The other arm of the same `except`: a refusal an extension authored is acknowledged."""
    dispatcher, seen = _dispatcher(Refuses())

    outcome = await dispatcher.handle_event(_message(), pattern="evt.a", raise_on_error=True)

    assert outcome == DispatchOutcome.OK
    assert seen == []

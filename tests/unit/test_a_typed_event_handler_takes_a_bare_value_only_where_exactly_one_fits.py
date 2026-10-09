"""A typed event handler whose message is not an object.

A listener's parameters become a payload model, and a message that is an object is validated against
it. A message that is not an object (a JSON `null`, a number) is not refused outright:

- it is the one value of a handler with exactly ONE parameter, `null` included;
- it is nothing for a handler with NO parameters, but only if it is `null`: a number sent to a handler
  that takes nothing is refused, not ignored;
- for a handler with two or more parameters it is refused, even if the others have defaults.

"Handled" is what the handler received; "refused" is the dead-letter step being run and the handler
never being called.
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
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.typed_events import build_event_spec

pytestmark = pytest.mark.unit


class _Owner:
    pass


def _setup(handler) -> tuple[EventDispatcher, MagicMock]:
    registry = ServiceRegistry()
    registry.event_handlers["evt.a"] = handler
    registry.event_specs_by_subject["evt.a"] = build_event_spec(
        handler.__name__, handler, owner=_Owner
    )
    config = ServiceConfig(name="events_svc", health_port=0)
    dlq = MagicMock(spec=DeadLetterPublisher)
    return EventDispatcher(registry, config, ExtensionPipeline([]), dlq), dlq


async def _deliver(dispatcher: EventDispatcher, body: bytes) -> DispatchOutcome:
    message = SimpleNamespace(subject="evt.a", data=body, headers={})
    return await dispatcher.handle_event(message, pattern="evt.a", raise_on_error=True)


def _no_params_handler(calls: list):
    def ping() -> None:
        calls.append("ping")

    return ping


async def test_a_null_message_is_nothing_for_a_handler_that_takes_nothing():
    calls: list = []
    dispatcher, dlq = _setup(_no_params_handler(calls))

    outcome = await _deliver(dispatcher, b"null")

    assert outcome == DispatchOutcome.OK
    assert calls == ["ping"]
    dlq.handle_invalid_message.assert_not_awaited()


@pytest.mark.parametrize("body", [b"5", b'"text"', b"[1]"])
async def test_any_other_non_object_is_refused_by_a_handler_that_takes_nothing(body):
    calls: list = []
    dispatcher, dlq = _setup(_no_params_handler(calls))

    outcome = await _deliver(dispatcher, body)

    assert outcome == DispatchOutcome.INVALID
    assert calls == []
    dlq.handle_invalid_message.assert_awaited_once()


async def test_CONTROL_an_empty_object_is_an_object_for_a_handler_with_one_parameter():
    """`{}` fills no parameter and leaves the default; it is not taken as the one value."""
    received: list = []

    def on_count(count: int = 7) -> None:
        received.append(count)

    dispatcher, _ = _setup(on_count)

    outcome = await _deliver(dispatcher, b"{}")

    assert outcome == DispatchOutcome.OK
    assert received == [7]


async def test_a_number_is_the_one_value_of_a_handler_with_one_parameter():
    received: list = []

    def on_count(count: int) -> None:
        received.append(count)

    dispatcher, dlq = _setup(on_count)

    outcome = await _deliver(dispatcher, b"5")

    assert outcome == DispatchOutcome.OK
    assert received == [5]
    dlq.handle_invalid_message.assert_not_awaited()


async def test_a_null_message_is_the_one_value_of_a_handler_with_one_nullable_parameter():
    received: list = []

    def on_count(count: int | None) -> None:
        received.append(count)

    dispatcher, dlq = _setup(on_count)

    outcome = await _deliver(dispatcher, b"null")

    assert outcome == DispatchOutcome.OK
    assert received == [None]
    dlq.handle_invalid_message.assert_not_awaited()


async def test_a_number_is_refused_by_a_handler_with_two_parameters_even_if_one_has_a_default():
    received: list = []

    def on_count(count: int, step: int = 3) -> None:
        received.append((count, step))

    dispatcher, dlq = _setup(on_count)

    outcome = await _deliver(dispatcher, b"5")

    assert outcome == DispatchOutcome.INVALID
    assert received == []
    dlq.handle_invalid_message.assert_awaited_once()


async def test_CONTROL_an_object_fills_the_parameter_of_a_handler_with_one_parameter():
    """`{"count": 5}` is read as the object it is, not as the one value of `count`."""
    received: list = []

    def on_count(count: int = 7) -> None:
        received.append(count)

    dispatcher, _ = _setup(on_count)

    outcome = await _deliver(dispatcher, b'{"count": 5}')

    assert outcome == DispatchOutcome.OK
    assert received == [5]

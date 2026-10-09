"""How an event is found and decoded before any handler sees it.

- A pattern the registry holds no handler for is `NO_HANDLER`: nothing is dispatched and nothing is
  dead-lettered. The same call for a registered pattern is the control.
- The wire format is read from the `Content-Type` header, found whatever its case. It is the FIRST
  such header that decides, and a header that is not a content type is never taken for one.
- A body needing a package this replica lacks (msgpack) is `INVALID` for a caller that does not ask for
  errors, with no dead letter, and an `ImportError` for one that does.

Each case is a msgpack body under a JSON default, so a misread content type is a decode failure that
shows as an outcome and not a silently different value.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cliffracer.core import validation
from cliffracer.core.dispatch import (
    DeadLetterPublisher,
    DispatchOutcome,
    EventDispatcher,
    ExtensionPipeline,
)
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit

MSGPACK_BODY = b"\x81\xa1n\x07"  # msgpack for {"n": 7}
JSON_BODY = b'{"n": 7}'
MSGPACK = "application/msgpack"
JSON = "application/json"


def _setup() -> tuple[EventDispatcher, list[int], MagicMock]:
    seen: list[int] = []

    def handler(n: int) -> None:
        seen.append(n)

    registry = ServiceRegistry()
    registry.event_handlers["evt.a"] = handler
    config = ServiceConfig(name="events_svc", health_port=0, serialization_format="json")
    dlq = MagicMock(spec=DeadLetterPublisher)
    return EventDispatcher(registry, config, ExtensionPipeline([]), dlq), seen, dlq


def _message(body: bytes, headers: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(subject="evt.a", data=body, headers=headers)


async def test_a_pattern_with_no_handler_is_no_handler_and_dispatches_nothing():
    dispatcher, seen, dlq = _setup()

    outcome = await dispatcher.handle_event(_message(JSON_BODY, {}), pattern="evt.unregistered")

    assert outcome == DispatchOutcome.NO_HANDLER
    assert seen == []
    dlq.dead_letter_decode_error.assert_not_awaited()


async def test_CONTROL_a_registered_pattern_is_dispatched():
    dispatcher, seen, _ = _setup()

    outcome = await dispatcher.handle_event(_message(JSON_BODY, {}), pattern="evt.a")

    assert outcome == DispatchOutcome.OK
    assert seen == [7]


@pytest.mark.parametrize("name", ["Content-Type", "content-type", "CONTENT-TYPE"])
async def test_the_content_type_header_is_found_whatever_its_case(name):
    dispatcher, seen, _ = _setup()

    outcome = await dispatcher.handle_event(
        _message(MSGPACK_BODY, {name: MSGPACK}), pattern="evt.a", raise_on_error=True
    )

    assert outcome == DispatchOutcome.OK
    assert seen == [7]


async def test_a_header_that_is_not_the_content_type_is_not_taken_for_it():
    dispatcher, seen, dlq = _setup()
    headers = {"X-Trace": "text/plain", "Content-Type": MSGPACK}

    outcome = await dispatcher.handle_event(
        _message(MSGPACK_BODY, headers), pattern="evt.a", raise_on_error=True
    )

    assert outcome == DispatchOutcome.OK
    assert seen == [7]
    dlq.dead_letter_decode_error.assert_not_awaited()


async def test_the_first_content_type_header_decides_when_two_differ_in_case():
    dispatcher, seen, dlq = _setup()
    headers = {"Content-Type": MSGPACK, "content-type": JSON}

    outcome = await dispatcher.handle_event(
        _message(MSGPACK_BODY, headers), pattern="evt.a", raise_on_error=True
    )

    assert outcome == DispatchOutcome.OK
    assert seen == [7]
    dlq.dead_letter_decode_error.assert_not_awaited()


async def test_CONTROL_the_other_order_is_decoded_as_json_and_the_msgpack_body_is_refused():
    """The case above must not pass because the LAST header wins and happens to agree: with the
    order reversed the first one is JSON, and a msgpack body is then undecodable."""
    dispatcher, seen, dlq = _setup()
    headers = {"content-type": JSON, "Content-Type": MSGPACK}

    outcome = await dispatcher.handle_event(_message(MSGPACK_BODY, headers), pattern="evt.a")

    assert outcome == DispatchOutcome.INVALID
    assert seen == []
    dlq.dead_letter_decode_error.assert_awaited_once()


async def test_a_body_needing_a_missing_package_is_invalid_with_no_dead_letter():
    dispatcher, seen, dlq = _setup()

    with patch.object(validation, "msgpack", None):
        outcome = await dispatcher.handle_event(
            _message(MSGPACK_BODY, {"Content-Type": MSGPACK}), pattern="evt.a"
        )

    assert outcome == DispatchOutcome.INVALID
    assert seen == []
    dlq.dead_letter_decode_error.assert_not_awaited()


async def test_CONTROL_a_caller_that_asks_for_errors_gets_the_import_error():
    dispatcher, seen, dlq = _setup()

    with patch.object(validation, "msgpack", None), pytest.raises(ImportError):
        await dispatcher.handle_event(
            _message(MSGPACK_BODY, {"Content-Type": MSGPACK}),
            pattern="evt.a",
            raise_on_error=True,
        )

    assert seen == []
    dlq.dead_letter_decode_error.assert_not_awaited()

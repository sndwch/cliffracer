"""An event handler is called with what its signature asks for, awaited only if it is a coroutine.

Four ways a handler reaches the dispatcher, each with a plain `def` and an `async def`:

- a validated listener (a schema registered for the subject);
- a typed listener (an `EventHandlerSpec`);
- a handler with neither, which takes the payload's own fields, with or without `subject`.

For each, the value the handler RETURNS is what the extensions' `worker_result` sees, and an error in
calling it is raised to a caller that asks for errors. A plain `def` handler that was awaited would
have run and then failed on `await None`, which a log line alone would not show.

For the handler with neither, the payload is handed over by these rules, which are pinned here:

- the payload's fields are the handler's arguments; a handler that asks for a parameter called `data`
  and sits behind an envelope also gets the envelope's whole payload as `data`, alongside any field
  it names, and a field of the payload that is itself called `data` is not overwritten;
- a payload that is not an object is the single argument `data`;
- `correlation_id` is passed only to a handler that declares it, and is dropped for one that does not.
"""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from cliffracer.core.dispatch import DeadLetterPublisher, EventDispatcher, ExtensionPipeline
from cliffracer.core.extension import Extension
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.typed_events import build_event_spec

pytestmark = pytest.mark.unit


class Payload(BaseModel):
    n: int


class _Owner:
    pass


class Recorder(Extension):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[Any] = []
        self.errors: list[BaseException | None] = []

    async def worker_result(self, ctx, result, exc) -> None:
        self.results.append(result)
        self.errors.append(exc)


def _dispatcher(handler, *, schema=None, typed=False) -> tuple[EventDispatcher, Recorder]:
    recorder = Recorder()
    registry = ServiceRegistry()
    registry.event_handlers["evt.a"] = handler
    if schema is not None:
        registry.event_schemas["evt.a"] = (schema, None)
    if typed:
        registry.event_specs_by_subject["evt.a"] = build_event_spec(
            handler.__name__, handler, owner=_Owner
        )
    config = ServiceConfig(name="events_svc", health_port=0)
    dispatcher = EventDispatcher(
        registry, config, ExtensionPipeline([recorder]), MagicMock(spec=DeadLetterPublisher)
    )
    return dispatcher, recorder


async def _deliver(dispatcher: EventDispatcher, body: Any, headers: dict | None = None) -> None:
    message = SimpleNamespace(
        subject="evt.a", data=json.dumps(body).encode(), headers=headers or {}
    )
    await dispatcher.handle_event(message, pattern="evt.a", raise_on_error=True)


# --- the return value and the await, per route and per kind -------------------------------------


def validated(message: Payload):
    return "validated"


async def validated_async(message: Payload):
    return "validated"


def typed(n: int):
    return "typed"


async def typed_async(n: int):
    return "typed"


def with_subject(subject, n):
    return "with_subject"


async def with_subject_async(subject, n):
    return "with_subject"


def plain(n):
    return "plain"


async def plain_async(n):
    return "plain"


ROUTES = [
    pytest.param(validated, {"schema": Payload}, id="validated-sync"),
    pytest.param(validated_async, {"schema": Payload}, id="validated-async"),
    pytest.param(typed, {"typed": True}, id="typed-sync"),
    pytest.param(typed_async, {"typed": True}, id="typed-async"),
    pytest.param(with_subject, {}, id="subject-sync"),
    pytest.param(with_subject_async, {}, id="subject-async"),
    pytest.param(plain, {}, id="plain-sync"),
    pytest.param(plain_async, {}, id="plain-async"),
]


@pytest.mark.parametrize(("handler", "route"), ROUTES)
async def test_what_the_handler_returns_is_what_the_extensions_see_and_nothing_fails(
    handler, route
):
    dispatcher, recorder = _dispatcher(handler, **route)

    await _deliver(dispatcher, {"n": 1})

    assert recorder.errors == [None]
    assert recorder.results == [handler.__name__.removesuffix("_async")]


# --- how the payload reaches a handler that has neither schema nor spec -------------------------

ENVELOPE = {"source_service": "producer", "timestamp": "2026-01-01T00:00:00Z"}


async def test_a_payload_that_is_a_list_is_the_one_argument_data():
    received: list = []

    def on_event(data):
        received.append(data)

    dispatcher, _ = _dispatcher(on_event)

    await _deliver(dispatcher, [1, 2])

    assert received == [[1, 2]]


async def test_an_envelope_whose_payload_is_a_list_is_the_one_argument_data():
    received: list = []

    def on_event(data):
        received.append(data)

    dispatcher, _ = _dispatcher(on_event)

    await _deliver(dispatcher, {**ENVELOPE, "data": [1, 2]})

    assert received == [[1, 2]]


async def test_a_handler_asking_for_data_gets_the_envelopes_payload_whole():
    received: list = []

    def on_event(data):
        received.append(data)

    dispatcher, _ = _dispatcher(on_event)

    await _deliver(dispatcher, {**ENVELOPE, "data": {"y": 1}})

    assert received == [{"y": 1}]


async def test_a_handler_asking_for_data_and_a_field_gets_both():
    received: list = []

    def on_event(data, x=None):
        received.append((data, x))

    dispatcher, _ = _dispatcher(on_event)

    await _deliver(dispatcher, {**ENVELOPE, "data": {"x": 1}})

    assert received == [({"x": 1}, 1)]


async def test_a_field_called_data_in_the_payload_is_not_overwritten_by_the_payload():
    received: list = []

    def on_event(data, x):
        received.append((data, x))

    dispatcher, _ = _dispatcher(on_event)

    await _deliver(dispatcher, {**ENVELOPE, "data": {"data": "inner", "x": 1}})

    assert received == [("inner", 1)]


async def test_CONTROL_a_bare_payload_is_the_handlers_fields_and_not_data():
    received: list = []

    def on_event(x, data="unset"):
        received.append((x, data))

    dispatcher, _ = _dispatcher(on_event)

    await _deliver(dispatcher, {"x": 1})

    assert received == [(1, "unset")]


async def test_a_correlation_id_in_the_payload_is_dropped_for_a_handler_that_does_not_declare_it():
    received: list = []

    def on_event(n):
        received.append(n)

    dispatcher, _ = _dispatcher(on_event)

    await _deliver(dispatcher, {"n": 1, "correlation_id": "corr_inpayload0000001"})

    assert received == [1]


async def test_CONTROL_a_handler_that_declares_the_correlation_id_gets_the_messages():
    received: list = []

    def on_event(n, correlation_id):
        received.append((n, correlation_id))

    dispatcher, _ = _dispatcher(on_event)

    await _deliver(dispatcher, {"n": 1, "correlation_id": "corr_inpayload0000001"})

    assert received == [(1, "corr_inpayload0000001")]

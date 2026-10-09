"""The signature of an event handler is read once, for the listener on a service class as for a function.

A listener declared on a service class is registered as a bound method, and a bound method takes
no attributes. The dispatcher used to keep a handler's parsed signature as an attribute of the
handler, so for a method the assignment raised, was swallowed, and the signature was parsed again
for every message. The dispatcher now keeps it in a table of its own.
"""

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.dispatch import events
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

SUBJECT = "things.happened"


class Listening(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="probe", health_port=0))
        self.seen: list[int] = []

    @listener(SUBJECT, fanout=True)
    async def on_thing(self, subject: str, x: int = 0) -> None:
        self.seen.append(x)


@pytest.fixture
def constructions(monkeypatch):
    built: list[object] = []
    original = events._HandlerMeta.__init__

    def counting(self, *args, **kwargs):
        built.append(self)
        original(self, *args, **kwargs)

    monkeypatch.setattr(events._HandlerMeta, "__init__", counting)
    return built


def _discovered():
    service = Listening()
    service.container.discover_handlers()
    handler = service.container.registry.event_handlers[SUBJECT]
    return service, service.container.event_dispatcher, handler


def test_the_registered_listener_is_a_bound_method():
    """The case this file is about: a plain function would have worked before."""
    _service, _dispatcher, handler = _discovered()

    assert type(handler).__name__ == "method"


def test_two_lookups_of_a_listeners_signature_build_one_and_return_it_twice(constructions):
    _service, dispatcher, handler = _discovered()

    first = dispatcher._get_handler_meta(handler)
    second = dispatcher._get_handler_meta(handler)

    assert second is first
    assert len(constructions) == 1, len(constructions)


async def test_three_messages_to_a_listener_read_its_signature_once_and_all_arrive(constructions):
    service, dispatcher, _handler = _discovered()

    for x in (1, 2, 3):
        await dispatcher.handle_event(
            MockMessage(SUBJECT, json.dumps({"x": x}).encode(), reply=None), pattern=SUBJECT
        )

    assert service.seen == [1, 2, 3]
    assert len(constructions) == 1, len(constructions)


def test_CONTROL_a_plain_function_is_read_once_too(constructions):
    _service, dispatcher, _handler = _discovered()

    def plain(subject: str, x: int = 0) -> None: ...

    assert dispatcher._get_handler_meta(plain) is dispatcher._get_handler_meta(plain)
    assert len(constructions) == 1, len(constructions)


def test_the_signature_of_one_listener_is_not_served_for_another(constructions):
    _service, dispatcher, handler = _discovered()

    def other(subject: str, y: str) -> None: ...

    assert dispatcher._get_handler_meta(handler) is not dispatcher._get_handler_meta(other)
    assert len(constructions) == 2, len(constructions)


def test_a_handler_that_cannot_be_hashed_still_has_its_signature_read():
    class Unhashable:
        __hash__ = None  # type: ignore[assignment]

        def __call__(self, subject: str, x: int = 0) -> None: ...

    _service, dispatcher, _handler = _discovered()
    handler = Unhashable()

    meta = dispatcher._get_handler_meta(handler)

    assert meta.param_names == {"subject", "x"}


class Reading(BaseModel):
    value: int


class Validating(CliffracerService):
    """Two validated listeners of one class, whose signatures ask for different things."""

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="probe", health_port=0))
        self.got: dict[str, dict] = {}

    @validated_listener("readings.a", Reading, fanout=True)
    async def with_subject(self, message: Reading, subject: str) -> None:
        self.got["a"] = {"value": message.value, "subject": subject}

    @validated_listener("readings.b", Reading, fanout=True)
    async def with_correlation_id(self, message: Reading, correlation_id: str) -> None:
        self.got["b"] = {"value": message.value, "correlation_id": correlation_id}


async def test_two_validated_listeners_of_different_signatures_each_get_their_own():
    """One handler's signature is never served for another, whatever the handlers have in common.

    A table keyed by the handler's type, not the handler, gives every listener on a service the
    first one's signature: the second is then called with `subject=` it does not take.
    """
    service = Validating()
    service.container.discover_handlers()
    dispatcher = service.container.event_dispatcher

    for pattern, value in (("readings.a", 1), ("readings.b", 2), ("readings.a", 3)):
        outcome = await dispatcher.handle_event(
            MockMessage(
                pattern,
                json.dumps({"value": value}).encode(),
                headers={"X-Correlation-ID": f"cid-{value}"},
                reply=None,
            ),
            pattern=pattern,
            raise_on_error=True,
        )
        assert outcome.value == "ok", (pattern, outcome)

    assert service.got == {
        "a": {"value": 3, "subject": "readings.a"},
        "b": {"value": 2, "correlation_id": "cid-2"},
    }

"""A handler's signature is inspected once, however many messages it handles.

`_HandlerMeta` runs `inspect.signature` and derives what the dispatcher needs to know of the handler
(async or not, whether it takes `subject`, `correlation_id`, `data`, `**kwargs`). That is per handler
and not per message, so the result is kept on the handler after the first use. What is counted here is
how many times the dispatcher builds one, which is what the cache is for; a cache that is never filled
or never read costs a signature inspection on every message.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cliffracer.core.dispatch import DeadLetterPublisher, EventDispatcher, ExtensionPipeline, events
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def built(monkeypatch) -> list[object]:
    """Every `_HandlerMeta` the dispatcher builds, in order."""
    made: list[object] = []
    real = events._HandlerMeta

    class Counting(real):
        def __init__(self, handler):
            super().__init__(handler)
            made.append(handler)

    monkeypatch.setattr(events, "_HandlerMeta", Counting)
    return made


def _dispatcher(**handlers) -> EventDispatcher:
    registry = ServiceRegistry()
    registry.event_handlers.update({f"evt.{name}": fn for name, fn in handlers.items()})
    config = ServiceConfig(name="events_svc", health_port=0)
    return EventDispatcher(
        registry, config, ExtensionPipeline([]), MagicMock(spec=DeadLetterPublisher)
    )


def _message(name: str) -> SimpleNamespace:
    return SimpleNamespace(subject=f"evt.{name}", data=b'{"n": 1}', headers={})


async def test_three_messages_to_one_handler_read_its_signature_once(built):
    seen: list[int] = []

    def first(n: int) -> None:
        seen.append(n)

    dispatcher = _dispatcher(first=first)

    for _ in range(3):
        await dispatcher.handle_event(_message("first"), pattern="evt.first", raise_on_error=True)

    assert seen == [1, 1, 1], "fixture: the handler must have been called for each message"
    assert built == [first]


async def test_CONTROL_two_handlers_are_each_read_once(built):
    """A cache shared by every handler would read the first and hand its answer to the second."""
    seen: list[str] = []

    def first(n: int) -> None:
        seen.append("first")

    def second(n: int, subject: str) -> None:
        seen.append(f"second:{subject}")

    dispatcher = _dispatcher(first=first, second=second)

    for _ in range(2):
        await dispatcher.handle_event(_message("first"), pattern="evt.first", raise_on_error=True)
        await dispatcher.handle_event(_message("second"), pattern="evt.second", raise_on_error=True)

    assert seen == ["first", "second:evt.second"] * 2
    assert built == [first, second]

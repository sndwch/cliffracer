"""What an extension reads from `ctx.data` for an inbound event.

- `ctx.data["envelope"]` is the whole decoded message, and it is there only for a message that IS an
  envelope (it carries `data`, `source_service` and `timestamp`). A bare payload has none.
- `ctx.data["handler_name"]` is the name the registry gives the handler for the pattern, else the
  function's own `__name__`; a handler that has neither (a `functools.partial`) leaves the key absent
  and does not set it to `None`.

Read from a recording extension's `worker_result`, which sees the context the handler ran under.
"""

import functools
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cliffracer.core.dispatch import DeadLetterPublisher, EventDispatcher, ExtensionPipeline
from cliffracer.core.extension import Extension
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit


class Recorder(Extension):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[dict] = []

    async def worker_result(self, ctx, result, exc) -> None:
        self.seen.append(dict(ctx.data))


def _dispatch_setup(handler, *, registered_name: str | None = None):
    recorder = Recorder()
    registry = ServiceRegistry()
    registry.event_handlers["evt.a"] = handler
    if registered_name is not None:
        registry.event_handler_names["evt.a"] = registered_name
    config = ServiceConfig(name="events_svc", health_port=0)
    dispatcher = EventDispatcher(
        registry, config, ExtensionPipeline([recorder]), MagicMock(spec=DeadLetterPublisher)
    )
    return dispatcher, recorder


async def _deliver(dispatcher: EventDispatcher, body: dict) -> None:
    message = SimpleNamespace(subject="evt.a", data=json.dumps(body).encode(), headers={})
    await dispatcher.handle_event(message, pattern="evt.a", raise_on_error=True)


def _record(n: int) -> None:
    return None


ENVELOPE = {"data": {"n": 1}, "source_service": "producer", "timestamp": "2026-01-01T00:00:00Z"}


async def test_an_enveloped_message_leaves_its_whole_envelope_on_the_context():
    dispatcher, recorder = _dispatch_setup(_record)

    await _deliver(dispatcher, ENVELOPE)

    assert [ctx.get("envelope") for ctx in recorder.seen] == [ENVELOPE]


async def test_a_bare_payload_leaves_no_envelope_on_the_context():
    dispatcher, recorder = _dispatch_setup(_record)

    await _deliver(dispatcher, {"n": 1})

    assert len(recorder.seen) == 1, "fixture: the extension must have seen the dispatch"
    assert "envelope" not in recorder.seen[0]


async def test_a_message_missing_any_one_envelope_field_is_a_bare_payload():
    """`data`, `source_service` and `timestamp` all, or it is not an envelope."""
    dispatcher, recorder = _dispatch_setup(lambda **kw: None)
    for missing in ("data", "source_service", "timestamp"):
        recorder.seen.clear()
        await _deliver(dispatcher, {k: v for k, v in ENVELOPE.items() if k != missing})
        assert len(recorder.seen) == 1
        assert "envelope" not in recorder.seen[0], f"an envelope with no {missing}"


async def test_the_registrys_name_for_the_handler_wins_over_the_functions_own():
    dispatcher, recorder = _dispatch_setup(_record, registered_name="Service.on_record")

    await _deliver(dispatcher, {"n": 1})

    assert [ctx.get("handler_name") for ctx in recorder.seen] == ["Service.on_record"]


async def test_CONTROL_with_no_registered_name_the_functions_own_name_is_used():
    dispatcher, recorder = _dispatch_setup(_record)

    await _deliver(dispatcher, {"n": 1})

    assert [ctx.get("handler_name") for ctx in recorder.seen] == ["_record"]


async def test_a_handler_with_no_name_at_all_leaves_the_key_absent():
    nameless = functools.partial(_record)
    assert getattr(nameless, "__name__", None) is None, "fixture: a partial has no __name__"
    dispatcher, recorder = _dispatch_setup(nameless)

    await _deliver(dispatcher, {"n": 1})

    assert len(recorder.seen) == 1
    assert "handler_name" not in recorder.seen[0]

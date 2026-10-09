"""RejectMessage caller path tests across non-RPC handlers.

Verifies RejectMessage behavior for async_rpc, listener, and JetStream events.
JetStream event refusal must be handled cleanly without triggering a retry loop
before message ack.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, async_rpc, listener, timer
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


class Refuser(Extension):
    async def setup(self, ctx):
        self.refusals = 0

    async def worker_setup(self, ctx):
        self.refusals += 1
        raise RejectMessage("refused-by-test")


def _js_msg(subject="events.ping", data=b'{"seq": 1}', num_delivered=1):
    msg = AsyncMock()
    msg.subject = subject
    msg.data = data
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    return msg


class _PlainEventSvc(CliffracerService):
    """The service with no extension installed: what its handler does unrefused."""

    def __init__(self, config):
        super().__init__(config)
        self.handled = []

    @listener("events.ping", durable="pinger")
    async def on_ping(self, subject: str, seq: int = 1) -> None:
        self.handled.append({"seq": seq})


class _EventSvc(_PlainEventSvc):
    refuser = Refuser()


def _js_config():
    return ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )


async def test_a_refused_jetstream_event_is_acked_and_never_naked_or_terminated():
    svc = _EventSvc(_js_config())
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _js_msg()
    await svc.container._handle_jetstream_event(msg)

    assert svc.refuser.refusals == 1, "the hook must have run"
    assert svc.handled == [], "a refused event must not reach the handler"

    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()
    msg.term.assert_not_awaited()


async def test_a_refused_core_nats_event_does_not_reach_the_handler():
    """The core-NATS path has no ack; the property is only that it is dropped
    quietly rather than raising out of the callback."""
    svc = _EventSvc(_js_config())
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _js_msg()
    await svc.container._handle_event(msg)

    # `handled == []` is also what a dispatch path that does nothing leaves, so
    # the hook must be shown to have run on THIS path. The core path has no ack
    # API and never naks, so there is nothing to assert about acknowledgement.
    assert svc.refuser.refusals == 1, "the hook must have run"
    assert svc.handled == []


async def test_the_same_core_nats_event_reaches_the_handler_when_nothing_refuses_it():
    """The control for the test above: 'did not reach the handler' means something
    only against a demonstrated 'would have reached it'."""
    svc = _PlainEventSvc(_js_config())
    await svc.container._setup_extensions()
    svc._discover_handlers()

    await svc.container._handle_event(_js_msg())

    assert svc.handled == [{"seq": 1}]


class _PlainAsyncSvc(CliffracerService):
    def __init__(self, config):
        super().__init__(config)
        self.handled = []

    @async_rpc
    async def fire(self, note: str = "") -> None:
        self.handled.append(note)


class _AsyncSvc(_PlainAsyncSvc):
    refuser = Refuser()


def _async_msg(note="n"):
    msg = AsyncMock()
    msg.subject = "s.async.fire"
    msg.data = b'{"note": "%s"}' % note.encode()
    msg.headers = {}
    return msg


async def test_a_refused_async_request_is_dropped_without_a_reply():
    svc = _AsyncSvc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _async_msg()
    await svc.container._handle_async_request(msg)

    assert svc.refuser.refusals == 1, "the hook must have run"
    assert svc.handled == [], "a refused async request must not reach the handler"
    # Fire-and-forget never replies, so this cannot fail on its own; it is kept
    # to say what the path promises. The assertions above decide.
    msg.respond.assert_not_awaited()


async def test_the_same_async_request_reaches_the_handler_when_nothing_refuses_it():
    """The control for the test above."""
    svc = _PlainAsyncSvc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    await svc.container._handle_async_request(_async_msg("n"))

    assert svc.handled == ["n"]


class _TimerSvc(CliffracerService):
    refuser = Refuser()

    def __init__(self, config):
        super().__init__(config)
        self.firings = 0

    @timer(interval=0.01)
    async def tick(self):
        self.firings += 1


async def test_a_refused_timer_firing_is_a_warning_not_a_fault_and_the_next_one_still_runs():
    # loguru does not reach `caplog`, so the log is read through a sink of its own.
    lines: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: lines.append((m.record["level"].name, m.record["message"])), level="DEBUG"
    )
    try:
        svc = _TimerSvc(ServiceConfig(name="s"))
        await svc.container._setup_extensions()
        svc._discover_handlers()

        t = svc._timers[0]
        t.service_instance = svc
        t.method_name = "tick"

        await t._execute_method()
        await t._execute_method()
    finally:
        logger.remove(sink)

    assert svc.firings == 0, "a refused firing must not run the method"
    assert svc.refuser.refusals == 2, (
        "the SECOND firing must still be attempted -- a refusal is not fatal to the timer"
    )
    # Each refusal is reported once per firing, at WARNING and with no traceback, with its reason.
    assert [m for lvl, m in lines if lvl == "WARNING"] == [
        "Timer method tick refused: refused-by-test"
    ] * 2
    assert [m for lvl, m in lines if lvl == "ERROR"] == []
    # A refusal is the firing being turned away, not the service being broken: it is counted
    # apart from errors, and a refused firing is not an execution.
    assert t.error_count == 0
    assert t.refusal_count == 2
    assert t.execution_count == 0

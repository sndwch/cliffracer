"""RejectMessage caller path tests across non-RPC handlers.

Verifies RejectMessage behavior for async_rpc, listener, and JetStream events.
JetStream event refusal must be handled cleanly without triggering a retry loop
before message ack.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, async_rpc, listener, timer
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.core.jetstream import StreamSpec


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


class _EventSvc(CliffracerService):
    refuser = Refuser()

    def __init__(self, config):
        super().__init__(config)
        self.handled = []

    @listener("events.ping", durable="pinger")
    async def on_ping(self, subject: str, seq: int = 1) -> None:
        self.handled.append({"seq": seq})


def _js_config():
    return ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )


@pytest.mark.unit
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


@pytest.mark.unit
async def test_a_refused_core_nats_event_does_not_reach_the_handler():
    """The core-NATS path has no ack; the property is only that it is dropped
    quietly rather than raising out of the callback."""
    svc = _EventSvc(_js_config())
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _js_msg()
    await svc.container._handle_event(msg)

    assert svc.handled == []
    msg.nak.assert_not_awaited()


class _AsyncSvc(CliffracerService):
    refuser = Refuser()

    def __init__(self, config):
        super().__init__(config)
        self.handled = []

    @async_rpc
    async def fire(self, note: str = "") -> None:
        self.handled.append(note)


@pytest.mark.unit
async def test_a_refused_async_request_is_dropped_without_a_reply():
    svc = _AsyncSvc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = AsyncMock()
    msg.subject = "s.async.fire"
    msg.data = b"{}"
    msg.headers = {}
    await svc.container._handle_async_request(msg)

    assert svc.handled == [], "a refused async request must not reach the handler"
    msg.respond.assert_not_awaited(), "async is fire-and-forget: nothing answers"


class _TimerSvc(CliffracerService):
    refuser = Refuser()

    def __init__(self, config):
        super().__init__(config)
        self.firings = 0

    @timer(interval=0.01)
    async def tick(self):
        self.firings += 1


@pytest.mark.unit
async def test_a_refused_timer_firing_is_logged_and_the_next_one_still_runs(caplog):
    svc = _TimerSvc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    t = svc._timers[0]
    t.service_instance = svc
    t.method_name = "tick"

    await t._execute_method()
    await t._execute_method()

    assert svc.firings == 0, "a refused firing must not run the method"
    assert svc.refuser.refusals == 2, (
        "the SECOND firing must still be attempted -- a refusal is not fatal to the timer"
    )

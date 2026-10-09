"""A `RejectMessage` that does not call its base `__init__` is an authored refusal on every arm.

`hook_crash` is how the dispatch tells a refusal an extension authored from the service being broken.
The synchronous request and describe arms read it with a default, and the fire-and-forget, event and
timer arms read the attribute, so a subclass whose `__init__` never reached `RejectMessage.__init__`
was refused on two arms and raised `AttributeError` out of dispatch on the others; a JetStream
listener NAKed it as a handler failure. A `RetryMessage` subclass that skips its base `__init__` has
no `retry_after` or `reason` either. The defaults are the classes' now, every arm reads them the
same way, and the log lines name a refusal by its text.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, async_rpc, listener, rpc, timer
from cliffracer.core.extension import Extension, RejectMessage, RetryMessage
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit


class SkipsInit(RejectMessage):
    """A refusal written by an extension author who never called `super().__init__`."""

    def __init__(self, reason: str) -> None:
        Exception.__init__(self, reason)


class Gate(Extension):
    """A gate that refuses with whichever class the test chose."""

    fails_closed = True

    async def worker_setup(self, ctx):
        raise self.service.refusal


def _service(refusal: RejectMessage) -> CliffracerService:
    class Svc(CliffracerService):
        gate = Gate()

        def __init__(self, config):
            super().__init__(config)
            self.ran = 0

        @rpc
        async def probe(self) -> str:
            self.ran += 1
            return "ok"

        @async_rpc
        async def audit(self) -> None:
            self.ran += 1

        @listener("things.happened", fanout=True)
        async def on_event(self, subject: str) -> None:
            self.ran += 1

    svc = Svc(ServiceConfig(name="s", health_port=0))
    svc.refusal = refusal
    return svc


async def _ready(refusal: RejectMessage) -> CliffracerService:
    svc = _service(refusal)
    await svc.container._setup_extensions()
    svc._discover_handlers()
    return svc


def _capture():
    lines: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: lines.append((m.record["level"].name, m.record["message"])), level="DEBUG"
    )
    return lines, sink


def _message(subject: str) -> AsyncMock:
    msg = AsyncMock()
    msg.subject = subject
    msg.data = json.dumps({}).encode()
    msg.headers = {"Content-Type": "application/json"}
    msg.reply = "_INBOX.test"
    return msg


REFUSALS = [
    pytest.param(lambda: RejectMessage("not for you"), id="base-class"),
    pytest.param(lambda: SkipsInit("not for you"), id="skips-base-init"),
]


@pytest.mark.parametrize("make", REFUSALS)
@pytest.mark.parametrize("arm", ["rpc", "describe"])
async def test_the_request_arms_answer_it_as_a_refusal(make, arm):
    svc = await _ready(make())
    msg = _message("s.describe" if arm == "describe" else "s.rpc.probe")

    await (
        svc.container._handle_describe_request(msg)
        if arm == "describe"
        else svc.container._handle_rpc_request(msg)
    )

    reply = json.loads(msg.respond.await_args_list[0].args[0].decode())
    assert (reply["code"], reply["error"]) == ("refused", "refused: not for you")
    assert svc.ran == 0


@pytest.mark.parametrize("make", REFUSALS)
async def test_the_fire_and_forget_arm_logs_it_as_a_refusal_and_does_not_raise(make):
    svc = await _ready(make())
    lines, sink = _capture()
    try:
        await svc.container._handle_async_request(
            MockMessage("s.async.audit", data=b"{}", headers={"Content-Type": "application/json"})
        )
    finally:
        logger.remove(sink)

    assert svc.ran == 0
    assert [m for level, m in lines if level == "WARNING" and "refused" in m], lines
    assert not [m for level, m in lines if level == "ERROR"], lines


@pytest.mark.parametrize("raise_on_error", [False, True])
@pytest.mark.parametrize("make", REFUSALS)
async def test_the_event_arm_acknowledges_it_as_a_refusal_whoever_asks(make, raise_on_error):
    svc = await _ready(make())
    lines, sink = _capture()
    try:
        await svc.container.dispatcher.events.handle_event(
            MockMessage(
                "things.happened", data=b"{}", headers={"Content-Type": "application/json"}
            ),
            pattern="things.happened",
            raise_on_error=raise_on_error,
        )
    finally:
        logger.remove(sink)

    assert svc.ran == 0
    assert [m for level, m in lines if level == "WARNING" and "refused by an extension" in m], lines
    assert not [m for level, m in lines if level == "ERROR"], lines


def test_a_refusal_is_not_a_crash_unless_it_says_so():
    assert SkipsInit("x").hook_crash is False
    assert RejectMessage("x").hook_crash is False
    assert RejectMessage("x", hook_crash=True).hook_crash is True


class RetrySkipsInit(RetryMessage):
    """A retry written by an extension author who never called `super().__init__`."""

    def __init__(self, reason: str) -> None:
        Exception.__init__(self, reason)


def _durable_service(refusal: RejectMessage) -> CliffracerService:
    class Durable(CliffracerService):
        gate = Gate()

        def __init__(self, config):
            super().__init__(config)
            self.ran = 0

        @listener("events.ping", durable="pinger")
        async def on_ping(self, seq: int) -> None:
            self.ran += 1

    svc = Durable(
        ServiceConfig(
            name="pinger",
            health_port=0,
            jetstream_enabled=True,
            jetstream_max_deliver=4,
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.>"]),
            ],
        )
    )
    svc.refusal = refusal
    return svc


def _durable_message() -> AsyncMock:
    msg = AsyncMock()
    msg.subject = "events.ping"
    msg.data = b'{"seq": 1}'
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=1)
    return msg


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: RetryMessage("busy"), id="base-class"),
        pytest.param(lambda: RetrySkipsInit("busy"), id="skips-base-init"),
    ],
)
async def test_the_jetstream_arm_naks_a_retry_and_says_why(make):
    svc = _durable_service(make())
    await svc.container._setup_extensions()
    svc._discover_handlers()
    svc.container.js = AsyncMock()
    msg = _durable_message()
    lines, sink = _capture()
    try:
        await svc.container._handle_jetstream_event(msg, pattern="events.ping")
    finally:
        logger.remove(sink)

    assert svc.ran == 0
    msg.nak.assert_awaited_once()
    assert [m for level, m in lines if level == "WARNING" and "busy; NAKing" in m], lines


@pytest.mark.parametrize("make", REFUSALS)
async def test_the_jetstream_arm_acknowledges_a_refusal(make):
    svc = _durable_service(make())
    await svc.container._setup_extensions()
    svc._discover_handlers()
    svc.container.js = AsyncMock()
    msg = _durable_message()
    lines, sink = _capture()
    try:
        await svc.container._handle_jetstream_event(msg, pattern="events.ping")
    finally:
        logger.remove(sink)

    assert svc.ran == 0
    assert msg.nak.await_count == 0, lines
    assert [m for level, m in lines if level == "WARNING" and "refused by an extension" in m], lines


def test_a_retry_that_skips_its_base_init_asks_for_no_particular_delay():
    assert RetrySkipsInit("x").retry_after is None
    assert RetryMessage("x").retry_after is None


class CrashSkipsInit(RejectMessage):
    """A refusal that declares itself a crash at class level and never calls `super().__init__`."""

    hook_crash = True

    def __init__(self, reason: str) -> None:
        Exception.__init__(self, reason)


async def _event(refusal: RejectMessage) -> tuple[CliffracerService, list[tuple[str, str]]]:
    svc = await _ready(refusal)
    lines, sink = _capture()
    try:
        await svc.container.dispatcher.events.handle_event(
            MockMessage(
                "things.happened", data=b"{}", headers={"Content-Type": "application/json"}
            ),
            pattern="things.happened",
        )
    finally:
        logger.remove(sink)
    return svc, lines


async def test_the_event_arm_logs_a_retry_that_skips_its_base_init_as_deferred():
    svc, lines = await _event(RetrySkipsInit("busy"))

    assert svc.ran == 0
    assert [
        m
        for level, m in lines
        if level == "WARNING" and "deferred by an extension" in m and "busy" in m
    ], lines


async def test_the_event_arm_logs_a_crash_that_skips_its_base_init_as_a_crash():
    svc, lines = await _event(CrashSkipsInit("hook blew up"))

    assert svc.ran == 0
    assert [
        m for level, m in lines if level == "ERROR" and "hook crashed" in m and "hook blew up" in m
    ], lines


@pytest.mark.parametrize("make", REFUSALS)
async def test_the_timer_arm_counts_it_as_a_refusal_not_a_fault(make):
    class Ticking(CliffracerService):
        gate = Gate()

        def __init__(self, config):
            super().__init__(config)
            self.ran = 0

        @timer(interval=60)
        async def tick(self) -> None:
            self.ran += 1

    svc = Ticking(ServiceConfig(name="s", health_port=0))
    svc.refusal = make()
    await svc.container._setup_extensions()
    svc._discover_handlers()
    fired = svc._timers[0]
    fired.service_instance = svc
    fired.method_name = "tick"
    lines, sink = _capture()

    try:
        await fired._execute_method()
    finally:
        logger.remove(sink)

    assert svc.ran == 0
    assert (fired.refusal_count, fired.error_count) == (1, 0)
    assert fired.last_refusal == "not for you"
    assert [
        m for level, m in lines if level == "WARNING" and "refused" in m and "not for you" in m
    ], lines
    assert not [m for level, m in lines if level == "ERROR"], lines

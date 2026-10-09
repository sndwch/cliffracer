"""Claims the ADR notes make about the code, each held by a test here or one they name.

ADR-0005: an extension's `__init__` runs once for the declaration and once for each service.
ADR-0010: startup refuses a configuration or type contract before it connects, and a payload with
no content type is read as JSON or msgpack, whichever it is.
ADR-0006: a refused event leaves one log line and publishes nothing, and a send hook cannot refuse.
ADR-0012: the generator's dial.
ADR-0014: nats-py acknowledges with no reason, so a dead-letter record is the diagnostic channel.
ADR-0015: a callback, and a pull fetch, spawns a task for each message, and the exported decorator resets what it sets.
ADR-0016: the broker is judged from the client's flags, and a probe that does no I/O is accepted.
ADR-0017: the container reads a small, named surface of its service.
Packaging: core does not depend on aiohttp, which nats-py needs for a WebSocket URL.
"""

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, dependency, listener, rpc
from cliffracer.core.container import Container
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.core.jetstream import StreamSpec
from cliffracer.core.lifecycle import LifecycleHooks, LifecycleManager
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


# -- ADR-0005 ---------------------------------------------------------------------------------


def test_an_extension_init_runs_once_for_the_declaration_and_once_per_service():
    runs = {"n": 0}

    class Counting(Extension):
        def __init__(self) -> None:
            runs["n"] += 1
            super().__init__()

    class Svc(CliffracerService):
        counted = Counting()

    declared = runs["n"]
    for name in ("a", "b", "c"):
        Svc(ServiceConfig(name=name, health_port=0))

    assert (declared, runs["n"]) == (1, 4)


# -- ADR-0010 ---------------------------------------------------------------------------------


def _recording_hooks(calls: list[str], *, jetstream: bool) -> LifecycleHooks:
    def sync(name: str):
        return lambda: calls.append(name)

    def recording(name: str):
        async def hook() -> None:
            calls.append(name)

        return hook

    return LifecycleHooks(
        setup_extensions=recording("setup_extensions"),
        discover_handlers=sync("discover_handlers"),
        connect=recording("connect"),
        ensure_streams=recording("ensure_streams"),
        validate_dlq=sync("validate_dlq"),
        is_jetstream_active=lambda: jetstream,
        on_startup=recording("on_startup"),
        start_extensions=recording("start_extensions"),
        start_health_listener=recording("start_health_listener"),
        start_timers=recording("start_timers"),
        setup_subscriptions=recording("setup_subscriptions"),
        stop_timers=recording("stop_timers"),
        stop_health_listener=recording("stop_health_listener"),
        cancel_subscriptions=recording("cancel_subscriptions"),
        on_shutdown=recording("on_shutdown"),
        stop_extensions=recording("stop_extensions"),
        disconnect=recording("disconnect"),
    )


async def test_startup_discovers_handlers_before_it_connects_and_before_any_user_code():
    calls: list[str] = []
    manager = LifecycleManager(
        ServiceConfig(name="ordered", health_listener=False),
        _recording_hooks(calls, jetstream=False),
    )

    await manager.start()

    assert calls == [
        "setup_extensions",
        "discover_handlers",
        "connect",
        "on_startup",
        "start_extensions",
        "start_health_listener",
        "start_timers",
        "setup_subscriptions",
    ]
    await manager.stop()


async def test_with_jetstream_the_streams_and_the_dead_letter_check_follow_the_connection():
    calls: list[str] = []
    manager = LifecycleManager(
        ServiceConfig(name="streams", health_listener=False),
        _recording_hooks(calls, jetstream=True),
    )

    await manager.start()

    assert calls[:5] == [
        "setup_extensions",
        "discover_handlers",
        "connect",
        "ensure_streams",
        "validate_dlq",
    ]
    assert calls.index("on_startup") == 5
    await manager.stop()


def test_a_payload_with_no_content_type_is_read_as_json_or_as_msgpack_whichever_it_is():
    import msgpack

    from cliffracer.core.validation import deserialize_payload

    packed = msgpack.packb({"a": 1})
    assert deserialize_payload(packed) == {"a": 1}
    assert deserialize_payload(b'{"a": 1}', None, "msgpack") == {"a": 1}


def test_a_payload_with_a_content_type_is_not_sniffed():
    import msgpack

    from cliffracer.core.validation import deserialize_payload

    with pytest.raises(UnicodeDecodeError):
        deserialize_payload(msgpack.packb({"a": 1}), "application/json")


# -- ADR-0006 ---------------------------------------------------------------------------------


async def test_a_refused_jetstream_event_leaves_one_warning_line_and_publishes_nothing():
    class Refuser(Extension):
        async def worker_setup(self, ctx) -> None:
            raise RejectMessage("not authorised")

    class Svc(CliffracerService):
        refuser = Refuser()

        @listener("events.ping", fanout=True)
        async def on_ping(self, seq: int) -> None: ...

    svc = Svc(
        ServiceConfig(
            name="refused",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="EVENTS", subjects=["events.*", "dlq.refused"])],
        )
    )
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc._discover_handlers()
    message = AsyncMock()
    message.subject, message.data, message.headers = "events.ping", b'{"seq": 1}', None
    message.metadata = SimpleNamespace(num_delivered=1)
    lines: list[tuple[str, str]] = []
    sink = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])))

    try:
        await svc.container._handle_jetstream_event(message, pattern="events.ping")
    finally:
        logger.remove(sink)

    refusals = [text for level, text in lines if "refused by an extension" in text]
    assert len(refusals) == 1, lines
    assert "events.ping" in refusals[0] and "not authorised" in refusals[0], refusals
    assert [level for level, text in lines if "refused by an extension" in text] == ["WARNING"]
    assert message.ack.await_count == 1
    # No dead-letter record, no reply, no header: a publish on either connection would be one.
    assert svc.js.publish.await_count == 0 and svc.nc.publish.await_count == 0


async def test_a_reject_message_from_before_call_is_logged_and_the_message_is_still_sent():
    class Refuser(Extension):
        async def before_call(self, ctx) -> None:
            raise RejectMessage("this send is refused")

    class Svc(CliffracerService):
        refuser = Refuser()

    svc = Svc(ServiceConfig(name="sender", health_port=0))
    svc.container.nc = AsyncMock()
    await svc.container._setup_extensions()

    await svc.publish_event("things.happened", x=1)

    assert svc.container.nc.publish.await_count == 1


# -- ADR-0012 ---------------------------------------------------------------------------------


async def test_the_generator_dial_is_bounded_by_its_timeout_and_one_reconnect():
    from cliffracer.generate_client import cli

    seen: dict[str, Any] = {}

    async def refused(url: str, **kwargs: Any) -> None:
        seen.update(kwargs)
        raise OSError("stop here")

    with patch("cliffracer.core.dial.connect", refused), pytest.raises(OSError, match="stop here"):
        await cli.fetch_description("nats://broker:4222", "svc", None, 4.0)

    assert seen["connect_timeout"] == 4.0
    assert seen["max_reconnect_attempts"] == 1


# -- ADR-0014 ---------------------------------------------------------------------------------


def test_nats_py_terminates_and_naks_with_no_reason():
    """The dead-letter record is the only place a reason for a termination can be written."""
    from nats.aio.msg import Msg

    assert list(inspect.signature(Msg.term).parameters) == ["self"]
    assert list(inspect.signature(Msg.nak).parameters) == ["self", "delay"]


# -- ADR-0015 ---------------------------------------------------------------------------------


async def test_the_rpc_event_and_jetstream_callbacks_each_spawn_a_task_for_the_message():
    class Svc(Cliffracer := CliffracerService):  # noqa: N801
        @rpc
        async def work(self) -> int:
            return 1

        @listener("things.happened", fanout=True)
        async def on_thing(self, subject: str) -> None: ...

    svc = Svc(ServiceConfig(name="spawns", health_port=0))
    spawned: list[str | None] = []

    def spawner(coro: Any, name: str | None = None) -> None:
        spawned.append(name)
        coro.close()

    dispatcher = svc.container.dispatcher
    for part in (dispatcher.rpc, dispatcher.events, dispatcher.jetstream):
        part.task_spawner = spawner
    message = MockMessage(subject="spawns.rpc.work", data=b"{}", headers={})

    await dispatcher.rpc.on_rpc_request(message)
    await dispatcher.events.make_event_callback("things.happened")(message)
    await dispatcher.jetstream.make_event_callback("things.happened")(message)

    assert spawned == ["rpc_request", "event:things.happened", "jetstream_event:things.happened"]


@pytest.mark.parametrize("max_event_concurrency", [None, 2])
async def test_a_pull_fetch_spawns_a_task_for_each_message(max_event_concurrency):
    """`pull_once` spawns at two sites: one with no concurrency bound and one that takes a permit of
    the semaphore `max_event_concurrency` makes first, so each is run."""

    class Svc(CliffracerService):
        @listener("things.happened", durable="things")
        async def on_thing(self, subject: str) -> None: ...

    svc = Svc(
        ServiceConfig(name="pulls", health_port=0, max_event_concurrency=max_event_concurrency)
    )
    spawned: list[str | None] = []

    def spawner(coro: Any, name: str | None = None) -> "asyncio.Future[None]":
        spawned.append(name)
        coro.close()
        finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        finished.set_result(None)
        return finished

    jetstream = svc.container.dispatcher.jetstream
    jetstream.task_spawner = spawner
    subscription = SimpleNamespace(
        fetch=AsyncMock(
            return_value=[MockMessage(subject="things.happened", data=b"{}", headers={})] * 3
        )
    )

    await jetstream.pull_once(subscription)

    assert spawned == ["jetstream_pull_event"] * 3


async def test_the_exported_correlation_decorator_restores_the_ambient_id_when_it_returns():
    from cliffracer import with_correlation_id
    from cliffracer.core.correlation import correlation_id_var

    @with_correlation_id
    async def handler(correlation_id: str | None = None) -> str | None:
        return correlation_id_var.get()

    before = correlation_id_var.get()

    inside = await handler(correlation_id="abc")

    assert inside == "abc"
    assert correlation_id_var.get() == before


# -- ADR-0016 ---------------------------------------------------------------------------------


class _Flags:
    """A NATS client that has its state flags and nothing else: any other use raises."""

    is_closed = False
    is_connected = True
    is_draining = False
    is_connecting = False

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"the client has no {name}: it has its state flags and nothing else")


class _Answers(_Flags):
    """A client whose flags say connected and whose PING is answered at once, counted."""

    def __init__(self) -> None:
        self.pings = 0
        self._pongs: list[Any] = []

    async def _send_ping(self, future: Any = None) -> None:
        self.pings += 1
        future.set_result(True)


async def test_the_broker_is_asked_for_a_round_trip_as_a_declared_dependency_is():
    svc = CliffracerService(ServiceConfig(name="asked", health_port=0))
    svc._running = True
    svc.nc = _Answers()

    health = await svc.health_check()

    assert svc.nc.pings == 1
    assert health["status"] == "healthy" and health["nats_connected"] is True
    assert isinstance(health["nats_rtt_ms"], float)


async def test_with_the_broker_probe_turned_off_the_flags_alone_judge_the_broker():
    svc = CliffracerService(ServiceConfig(name="passive", health_port=0, broker_probe_timeout=None))
    svc._running = True
    svc.nc = _Flags()

    health = await svc.health_check()

    assert health["status"] == "healthy" and health["nats_rtt_ms"] is None


async def test_a_probe_that_does_no_io_is_accepted_and_reports_the_dependency_up():
    class Svc(CliffracerService):
        @dependency("db", timeout=0.5)
        async def _db(self) -> bool:
            return True

    svc = Svc(ServiceConfig(name="trivial", health_port=0))
    svc._discover_handlers()
    svc._running = True
    svc.nc = _Flags()

    health = await svc.health_check()

    assert health["dependencies"]["db"]["ok"] is True


# -- ADR-0017 ---------------------------------------------------------------------------------

#: What the container reads from the service it runs, and nothing else: the four lifecycle
#: overrides and `logger`, `health_listener` and `_running`, and the extension attributes it sets.
SERVICE_SURFACE = {
    "logger",
    "health_listener",
    "_running",
    "connect",
    "disconnect",
    "on_startup",
    "on_shutdown",
    "__class__",
}


class _Recording:
    """A stand-in service that records every attribute the container reads from it."""

    def __init__(self) -> None:
        object.__setattr__(self, "read", set())

    def __getattribute__(self, name: str) -> Any:
        if name not in ("read", "_container") and (
            not name.startswith("__") or name == "__class__"
        ):
            object.__getattribute__(self, "read").add(name)
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    async def connect(self) -> None:
        await object.__getattribute__(self, "_container").connect()

    async def disconnect(self) -> None:
        await object.__getattribute__(self, "_container").disconnect()

    async def on_startup(self) -> None: ...

    async def on_shutdown(self) -> None: ...


def _connected_client() -> MagicMock:
    nc = MagicMock(is_connected=True, is_closed=False, is_draining=False, is_connecting=False)
    for name in ("drain", "close", "flush", "subscribe"):
        setattr(nc, name, AsyncMock())
    return nc


async def test_a_container_with_no_service_starts_and_stops():
    container = Container(None, ServiceConfig(name="alone", health_port=0, health_listener=False))

    with patch("cliffracer.core.dial.connect", new=AsyncMock(return_value=_connected_client())):
        await container.start()
        assert container.is_running
        await container.stop()

    assert not container.is_running


async def test_a_container_reads_only_the_named_surface_of_its_service():
    service = _Recording()
    container = Container(
        service, ServiceConfig(name="named", health_port=0, health_listener=False)
    )
    object.__setattr__(service, "_container", container)

    with patch("cliffracer.core.dial.connect", new=AsyncMock(return_value=_connected_client())):
        await container.lifecycle.start()
        await container.lifecycle.stop()

    assert service.read <= SERVICE_SURFACE, sorted(service.read - SERVICE_SURFACE)
    assert {"connect", "disconnect", "on_startup", "on_shutdown"} <= service.read


# -- Packaging --------------------------------------------------------------------------------


def test_core_does_not_depend_on_aiohttp_and_nats_py_needs_it_for_a_websocket_transport():
    import tomllib
    from pathlib import Path

    import nats.aio.transport as transport

    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    assert not [d for d in pyproject["project"]["dependencies"] if d.lower().startswith("aiohttp")]
    with patch.object(transport, "aiohttp", None), pytest.raises(ImportError, match="aiohttp"):
        transport.WebSocketTransport()


# -- ADR-0015: the ambient correlation id outside the pipeline ---------------------------------


async def test_the_dead_letter_publisher_falls_back_to_the_ambient_correlation_id_when_given_none():
    """Code outside the dispatch pipeline may read the ambient id; an explicit id still wins."""
    import json

    from cliffracer.core.correlation import CorrelationContext
    from cliffracer.core.dispatch.dlq import DeadLetterPublisher

    published: list[bytes] = []

    class Wire:
        async def publish(self, subject, payload, headers=None):
            published.append(payload)

    publisher = DeadLetterPublisher(
        ServiceConfig(name="dead", health_port=0),
        lambda: SimpleNamespace(nc=Wire(), js=None, jetstream_active=False),
    )
    CorrelationContext.set("ambient-id")
    try:
        await publisher.publish_dlq("dlq.dead", original_subject="a.b")
        await publisher.publish_dlq(
            "dlq.dead", original_subject="a.b", correlation_id="explicit-id"
        )
    finally:
        CorrelationContext.clear()

    assert [json.loads(p)["correlation_id"] for p in published] == ["ambient-id", "explicit-id"]

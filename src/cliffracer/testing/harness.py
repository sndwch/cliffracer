"""In-memory test harness and dispatch runner for service handlers."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from cliffracer.core.clock import Clock
from cliffracer.core.construction import construct_service
from cliffracer.core.container import Container, DispatchOutcome
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.validation import serialize_payload
from cliffracer.testing.clock import FakeClock
from cliffracer.testing.jetstream import MockJetStreamContext
from cliffracer.testing.messages import MockJetStreamMetadata, MockMessage, TestResponse


async def _answer_the_round_trip(future: asyncio.Future[Any] | None = None) -> None:
    """The PONG a broker that is up sends: resolve the round trip's future."""
    if future is not None and not future.done():
        future.set_result(True)


class ServiceTestHarness:
    """In-memory service test harness.

    Orchestrates Container initialization, extension lifecycle, and message
    dispatch using mock message envelopes. Never connects to a live NATS broker.

    Given ``broker=``, an object whose ``async connect(url, **options)`` returns a
    connection, the harness instead runs the service's own ``start()`` and
    ``stop()`` over a connection from that broker, so several harnesses on one
    broker are several services talking to each other. That is the whole start
    sequence, including the health listener: with the harness's default
    ``health_port=0`` each started service binds one ephemeral loopback port,
    released by ``teardown()``. Without ``broker=`` the harness opens no socket.
    """

    def __init__(
        self,
        service: CliffracerService | type[CliffracerService],
        *,
        config: ServiceConfig | None = None,
        broker: Any = None,
    ) -> None:
        if isinstance(service, type):
            cfg = config or ServiceConfig(name="test_harness_svc", health_port=0)
            self._service = construct_service(service, cfg)
        elif config is not None:
            raise TypeError(
                "config= configures a service class the harness constructs. This harness "
                "was given an already-built instance, whose config is its own: pass the "
                "class instead, or build the instance with the config you want."
            )
        else:
            self._service = service

        self._container: Container = self._service.container
        self._broker = broker
        self._initialized = False
        self._torn_down = False
        self._timers_started = False
        self._mock_js = MockJetStreamContext()
        if broker is not None:
            self._container.connection.dial = broker.connect
            return
        self._mock_nc = AsyncMock()
        self._mock_nc.is_connected = True
        self._mock_nc.is_closed = False
        self._mock_nc.is_draining = False
        # The broker state also reads these two, defaulting to False. Left unset, each is a child
        # mock, which is truthy, and a connection marked down would read as connecting.
        self._mock_nc.is_connecting = False
        self._mock_nc.is_reconnecting = False
        # Readiness asks the broker for a round trip through `_send_ping`. An AsyncMock's never
        # resolves the future it is handed, so `health_check()` would wait out
        # `broker_probe_timeout` and report a connected service disconnected. The in-memory
        # connection answers at once, as a broker that is up does.
        self._mock_nc._send_ping = _answer_the_round_trip
        if not self._container.nc:
            self._container.nc = self._mock_nc
        # A JetStream-enabled service publishes through the context and refuses a
        # subject no declared stream covers. Leaving the context unset turns both
        # off, so the service under test would take the core path whatever its
        # config said, and a guard that fires against a broker would not fire here.
        if self._service.config.jetstream_enabled and self._container.js is None:
            # A stand-in rather than a JetStreamContext: the service reaches it
            # only through publish(), and the harness exists to keep a real one
            # off the wire.
            self._container.js = self._mock_js  # type: ignore[assignment]

    @property
    def service(self) -> CliffracerService:
        """The service instance under test."""
        return self._service

    def _require_jetstream(self, surface: str) -> None:
        """Refuse a JetStream question this harness cannot answer.

        With ``jetstream_enabled`` false the context is never attached to the
        container, so the service takes the core path and nothing it publishes
        goes over JetStream. Answering anyway lets a test assert a JetStream
        outcome for a path the service under test never takes: an empty publish
        history reads as "nothing went out over JetStream" rather than as "there
        was no JetStream", and the opposite assertion cannot pass however the
        service behaves.
        """
        if not self._service.config.jetstream_enabled:
            raise RuntimeError(
                f"{surface} answers for JetStream, and this harness wraps "
                f"{self._service.config.name!r} with jetstream_enabled=False. The "
                "context is never attached to the container, so the service takes "
                "the core path and nothing it publishes reaches JetStream. Set "
                "jetstream_enabled=True on the config to make the claim mean "
                "something, or assert on the core path instead."
            )

    @property
    def jetstream(self) -> MockJetStreamContext:
        """The in-memory JetStream context this harness installed.

        Carries what the service published, so a test can assert on the subjects
        and payloads that went out over JetStream.

        Only a service that enables JetStream has one. With JetStream off this
        refuses rather than handing back a context it did not install.
        """
        self._require_jetstream("harness.jetstream")
        if self._broker is not None:
            raise RuntimeError(
                "harness.jetstream is the recording context a harness without a broker "
                "installs. This harness runs over the broker it was given, so what the "
                "service published is on that broker: read it there."
            )
        return self._mock_js

    @property
    def container(self) -> Container:
        """The container instance bound to the service."""
        return self._container

    async def __aenter__(self) -> ServiceTestHarness:
        await self.setup()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.teardown()

    async def setup(self) -> None:
        """Set up extensions, discover handlers, run `on_startup`, start extensions, mark running.

        The hooks run in the order a live start runs them: extension ``setup()``, handler
        discovery, the service's own ``on_startup``, then extension ``start()``. A service whose
        ``on_startup`` builds what its handlers use has it by the time a message is dispatched,
        and an extension that spawns its background work in ``start()`` is running too.

        A hook that raises ends the setup: the error propagates, what had been started is
        wound down as ``teardown()`` would (``on_shutdown`` only if ``on_startup`` returned),
        and the harness is spent, like one that was torn down.
        """
        if self._torn_down:
            raise RuntimeError(
                "this harness has been torn down: its extensions are stopped and its "
                "service is no longer running. Build a new ServiceTestHarness rather "
                "than reusing this one."
            )
        if not self._initialized and self._broker is not None:
            try:
                await self._service.start()
            except BaseException:
                self._torn_down = True
                raise
            if not self._service.container.is_running:
                self._torn_down = True
                raise RuntimeError(
                    f"{self._service.config.name!r} did not start over the broker: its start "
                    "sequence returned without the service running"
                )
            self._initialized = True
        if not self._initialized:
            lifecycle = self._container.lifecycle
            await self._container._setup_extensions()
            try:
                self._container.discover_handlers()
                await self._container._on_service_startup()
                lifecycle._on_startup_completed = True
                lifecycle._running = True
                await self._container._start_extensions()
            except BaseException:
                await self._release(drain_timeout=5.0)
                self._torn_down = True
                raise
            self._initialized = True

    async def _release(self, *, drain_timeout: float) -> None:
        """Wind down what a setup, or a setup that failed, had started.

        The service's ``on_shutdown`` runs only for an ``on_startup`` that returned, and the
        extensions are stopped whether or not it raises, so a failing hook cannot leave them
        running; its error propagates once they are stopped.
        """
        lifecycle = self._container.lifecycle
        lifecycle._running = False
        try:
            if self._timers_started:
                self._timers_started = False
                await self._container._stop_timers()
            await lifecycle.drain_active_tasks(timeout=drain_timeout)
            if lifecycle._on_startup_completed:
                lifecycle._on_startup_completed = False
                await self._container._on_service_shutdown()
        finally:
            await self._container._stop_extensions()

    async def start_timers(self, *, clock: Clock | None = None) -> None:
        """Set the harness up if it is not, then start the service's timers.

        `setup()` leaves the timers stopped; a test that wants them firing starts them here, and
        `teardown()` stops them. With `clock`, every timer the service declared reads time and
        waits through it, typically a `FakeClock` the test then advances. A `FakeClock` is told
        about each timer's task, so its first `advance` waits for each timer to reach its first
        wait. Without `clock`, each timer keeps its own.
        """
        if self._broker is not None:
            raise RuntimeError(
                "timers run under start() on a broker harness: this harness starts the service "
                "itself, timers included, so start_timers would start them a second time. Give "
                "a timer its clock where it is declared, or use a harness without a broker."
            )
        if clock is not None and not isinstance(clock, Clock):
            raise TypeError(
                f"clock must have monotonic(), now(tz), sleep() and wait(), got {clock!r}"
            )
        await self.setup()
        timers = self._container.registry.timers
        if clock is not None:
            for timer in timers:
                timer.clock = clock
        self._timers_started = True
        await self._container._start_timers()
        if isinstance(clock, FakeClock):
            for timer in timers:
                clock.watch(timer.task)

    async def teardown(self, *, drain_timeout: float = 5.0) -> None:
        """Drain in-flight tasks within *drain_timeout*, run `on_shutdown`, then stop extensions.

        The drain is the framework's own ``drain_active_tasks``, which cancels
        whatever is still running when the deadline passes, so a task that never
        finishes fails the test rather than hanging the run. ``on_shutdown`` runs
        once, and only for a harness whose ``on_startup`` returned; a harness that
        never started runs neither hook.
        """
        if self._initialized and self._broker is not None:
            self._initialized = False
            self._torn_down = True
            await self._service.stop()
        if self._initialized:
            self._initialized = False
            self._torn_down = True
            await self._release(drain_timeout=drain_timeout)
        self._torn_down = True

    async def rpc(
        self,
        method: str,
        *,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        format: str = "json",
        **kwargs: Any,
    ) -> TestResponse:
        """Invoke an RPC method handler and return the decoded TestResponse."""
        await self.setup()
        data = payload if payload is not None else kwargs
        raw_bytes, content_type = serialize_payload(data, format=format)
        req_headers = dict(headers or {})
        req_headers["Content-Type"] = content_type

        subject = self._container._with_namespace(f"{self._service.config.name}.rpc.{method}")
        msg = MockMessage(
            subject=subject,
            data=raw_bytes,
            headers=req_headers,
        )
        await self._container.dispatcher.handle_rpc_request(msg)
        return TestResponse.from_mock_message(msg)

    async def emit_event(
        self,
        subject: str,
        data: Any = None,
        *,
        headers: dict[str, str] | None = None,
        format: str = "json",
        pattern: str | None = None,
        raise_on_error: bool = True,
        **kwargs: Any,
    ) -> DispatchOutcome:
        """Emit an event through the service container dispatch pipeline.

        ``subject`` is namespaced the way ``rpc()`` namespaces its own, so it
        reads as the subject written in the ``@listener`` decorator. ``pattern``
        names a registry key directly and is passed through verbatim.

        A subject that matches no handler returns
        ``DispatchOutcome.NO_HANDLER``, so a test that routes nowhere is
        distinguishable from one that ran a handler. An exception raised inside
        a handler propagates unless ``raise_on_error`` is set to False, and so
        does a `fails_closed` extension hook crashing -- that is the service
        being broken, and a test that asked for errors wants to hear it. A
        policy ``RejectMessage`` an extension authored is not an error: the
        handler is skipped and the outcome returns normally.
        """
        await self.setup()
        payload = data if data is not None else kwargs
        raw_bytes, content_type = serialize_payload(payload, format=format)
        req_headers = dict(headers or {})
        req_headers["Content-Type"] = content_type

        msg = MockMessage(
            subject=self._container._with_namespace(subject),
            data=raw_bytes,
            headers=req_headers,
        )
        return await self._container.dispatcher.handle_event(
            msg, pattern=pattern, raise_on_error=raise_on_error
        )

    async def deliver_jetstream(
        self,
        subject: str,
        data: Any = None,
        *,
        num_delivered: int = 1,
        headers: dict[str, str] | None = None,
        format: str = "json",
        pattern: str | None = None,
        **kwargs: Any,
    ) -> MockMessage:
        """Deliver a message through the JetStream dispatch path and return it.

        This is the path that decides acknowledgement: the returned message
        records whether it was acked, naked or terminated, so a test can assert
        on the retry and dead-letter policy rather than only on the handler.

        ``num_delivered`` is the delivery attempt this stands for. The policy
        terminates rather than redelivers once it reaches
        ``jetstream_max_deliver``, so a test of that boundary sets it here. No
        server consumer is read here, so the config's limit is the one that
        applies.

        Only a service that enables JetStream takes this path; with JetStream off
        this refuses rather than driving a dispatch path the service would never
        reach in the configuration under test.
        """
        self._require_jetstream("deliver_jetstream")
        await self.setup()
        payload = data if data is not None else kwargs
        raw_bytes, content_type = serialize_payload(payload, format=format)
        req_headers = dict(headers or {})
        req_headers["Content-Type"] = content_type

        msg = MockMessage(
            subject=self._container._with_namespace(subject),
            data=raw_bytes,
            headers=req_headers,
            metadata=MockJetStreamMetadata(num_delivered=num_delivered),
        )
        await self._container.dispatcher.jetstream.handle_jetstream_event(msg, pattern=pattern)
        return msg

    publish = emit_event

    async def describe(self) -> dict[str, Any]:
        """Invoke the service describe endpoint and return the description dict.

        Raises `RuntimeError` when the service sent no reply, or answered with a failure envelope,
        rather than returning a dict that reads as an empty description.
        """
        await self.setup()
        subject = self._container._with_namespace(f"{self._service.config.name}.describe")
        msg = MockMessage(subject=subject)
        await self._container.dispatcher.handle_describe_request(msg)
        resp = TestResponse.from_mock_message(msg)
        if not resp.raw_data:
            raise RuntimeError(
                f"describe() got no reply from {self._service.config.name!r}: the handler "
                f"produced none, or could not send it. An empty description would read as a "
                f"service with nothing in it."
            )
        if not isinstance(resp.data, dict):
            raise RuntimeError(f"describe() got a reply that is not an object: {resp.data!r}")
        if resp.data.get("success") is False:
            raise RuntimeError(
                f"describe() was answered with a failure, not a description: {resp.error!r}"
            )
        return resp.data

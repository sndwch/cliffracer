"""The in-memory broker these tests run on, and their fixtures.

Nothing here dials a broker. The tier runs on `cliffracer.testing.InMemoryBroker`, the one users
get; each behaviour it claims is a case in `tests/contract/transport_cases.py`, which also runs
against a real client. The suite's end-to-end coverage against a real broker lives in
tests/integration/test_typed_client_end_to_end.py.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any

import nats.aio.msg as natsmsg
import nats.errors
import pytest_asyncio

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.correlation import CorrelationContext, correlation_id_var
from cliffracer.testing import InMemoryBroker, ServiceTestHarness, refuse_a_reply_with_no_subject
from cliffracer.testing.broker import _Connection

#: A connection on the broker: what the `mock_transport` fixture yields, for annotations.
Connection = _Connection

__all__ = ["Connection", "MockJetStreamMsg", "started"]


class MockJetStreamMsg:
    """Simulated JetStream / NATS message with the acknowledgement rules of `nats.aio.msg.Msg`.

    A message built with no reply subject is a core message: it cannot be acknowledged, its
    `.metadata` raises `NotJSMessageError`, and a reply to it is refused, as the real one's are.
    `from_jetstream=True` builds a delivery from a stream: it has an ack subject for a reply, the
    metadata the real message parses from it (so `num_delivered`, `stream` and `consumer` are what
    that subject says), and acknowledges at most once. The counters record the acknowledgements
    that were sent; one the message refused is not counted.

    The carve-out tests that dispatch to the container directly build these; the broker delivers
    its own messages.
    """

    def __init__(
        self,
        subject: str,
        data: bytes,
        headers: dict[str, str] | None = None,
        reply: str | None = None,
        num_delivered: int = 1,
        stream: str = "DEFAULT",
        consumer: str = "test-consumer",
        transport: Any = None,
        from_jetstream: bool = False,
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = dict(headers) if headers else {}
        if from_jetstream and reply is None:
            reply = (
                f"$JS.ACK.{stream}.{consumer}.{num_delivered}.{num_delivered}.1."
                f"1700000000000000000.0"
            )
        self.reply = reply
        self._transport = transport
        self._metadata = natsmsg.Msg.Metadata._from_reply(reply) if from_jetstream else None
        self.ack_calls: int = 0
        self.nak_calls: list[float] = []
        self.term_calls: int = 0
        self.in_progress_calls: int = 0
        self._ackd: bool = False
        self._response_sent: bool = False
        self.respond_attempts: int = 0
        self.response_data: bytes | None = None

    @property
    def metadata(self) -> Any:
        """What `Msg.metadata` answers: the delivery's, or `NotJSMessageError` for a core message."""
        if self._metadata is None:
            raise nats.errors.NotJSMessageError
        return self._metadata

    def _check_reply(self) -> None:
        """The rules `Msg._check_reply` applies to a terminal acknowledgement."""
        if not self.reply:
            raise nats.errors.NotJSMessageError
        if self._ackd:
            raise nats.errors.MsgAlreadyAckdError(self)

    async def ack(self) -> None:
        self._check_reply()
        self.ack_calls += 1
        self._ackd = True

    async def nak(self, delay: float = 0.0) -> None:
        self._check_reply()
        self.nak_calls.append(delay)
        self._ackd = True

    async def term(self) -> None:
        self._check_reply()
        self.term_calls += 1
        self._ackd = True

    async def in_progress(self) -> None:
        if not self.reply:
            raise nats.errors.NotJSMessageError
        self.in_progress_calls += 1

    async def respond(self, data: bytes) -> None:
        self.respond_attempts += 1
        refuse_a_reply_with_no_subject(self)
        self._response_sent = True
        self.response_data = data
        if self._transport is not None:
            await self._transport.publish(self.reply, data, headers=self.headers or None)


@contextlib.asynccontextmanager
async def started(
    transport: Connection, *services: CliffracerService
) -> AsyncIterator[list[ServiceTestHarness]]:
    """Start each service with its own `start()`, over its own connection to `transport`'s broker.

    Each runs the whole start sequence a broker would see, subscriptions included, and is stopped
    with `stop()` on the way out, in the reverse order.
    """
    async with contextlib.AsyncExitStack() as stack:
        harnesses = [
            await stack.enter_async_context(ServiceTestHarness(svc, broker=transport.broker))
            for svc in services
        ]
        yield harnesses


@pytest_asyncio.fixture
async def mock_transport() -> AsyncGenerator[Connection]:
    """A connection on a fresh in-memory broker. `mock_transport.broker` hands out more."""
    connection = await InMemoryBroker().connect(name="test")
    yield connection
    await connection.close()


@pytest_asyncio.fixture
async def transport_service_factory() -> AsyncGenerator[Callable[..., CliffracerService]]:
    """Factory for spinning up isolated test services with clean lifecycle teardown."""
    created_services: list[CliffracerService] = []

    def _create(
        service_cls: type[CliffracerService] = CliffracerService,
        name: str = "test_transport_svc",
        **config_kwargs: Any,
    ) -> CliffracerService:
        # A started service binds its health listener; 0 asks for an ephemeral port, so two
        # services, or two runs on one host, never contend for one.
        config_kwargs.setdefault("health_port", 0)
        config = ServiceConfig(
            name=name,
            auto_restart=False,
            request_timeout=5.0,
            **config_kwargs,
        )
        svc = service_cls(config)
        created_services.append(svc)
        return svc

    yield _create

    for svc in created_services:
        await svc.stop()


@pytest_asyncio.fixture(autouse=True)
async def _clean_test_context() -> Any:
    """Ensure correlation context is pristine before and after every transport test."""
    CorrelationContext.clear()
    token = correlation_id_var.set(None)
    try:
        yield
    finally:
        CorrelationContext.clear()
        correlation_id_var.reset(token)

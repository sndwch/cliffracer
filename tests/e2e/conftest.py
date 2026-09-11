"""E2E test fixtures and in-memory transport infrastructure for Cliffracer."""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.correlation import CorrelationContext, correlation_id_var

# Ensure packages/cliffracer-faststream/src is importable if it exists
faststream_src = Path(__file__).parents[2] / "packages" / "cliffracer-faststream" / "src"
if faststream_src.exists() and str(faststream_src) not in sys.path:
    sys.path.insert(0, str(faststream_src))


@dataclass
class MockJetStreamMetadata:
    """Mock JetStream message metadata."""

    sequence: int = 1
    num_delivered: int = 1
    stream: str = "DEFAULT"
    consumer: str = "test-consumer"
    timestamp: float = 0.0


class MockJetStreamMsg:
    """Simulated JetStream / NATS message with full acknowledgment lifecycle."""

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
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = dict(headers) if headers else {}
        self.reply = reply
        self._transport = transport
        self.metadata = MockJetStreamMetadata(
            num_delivered=num_delivered, stream=stream, consumer=consumer
        )
        self.ack_calls: int = 0
        self.nak_calls: list[float] = []
        self.term_calls: int = 0
        self.in_progress_calls: int = 0
        self._ackd: bool = False
        self._response_sent: bool = False
        self.response_data: bytes | None = None

    async def ack(self) -> None:
        self.ack_calls += 1
        self._ackd = True

    async def nak(self, delay: float = 0.0) -> None:
        self.nak_calls.append(delay)
        self._ackd = True

    async def term(self) -> None:
        self.term_calls += 1
        self._ackd = True

    async def in_progress(self) -> None:
        self.in_progress_calls += 1

    async def respond(self, data: bytes) -> None:
        if not self.reply:
            raise RuntimeError("no reply subject available")
        self._response_sent = True
        self.response_data = data
        if self._transport is not None:
            await self._transport.publish(self.reply, data)


class MockNatsTransport:
    """High-fidelity in-memory NATS transport for E2E tests without live daemon."""

    def __init__(self) -> None:
        self.subscriptions: dict[str, list[Callable[[MockJetStreamMsg], Any]]] = {}
        self.published_messages: list[tuple[str, bytes, dict[str, str] | None, str | None]] = []
        self.is_connected: bool = True
        self.is_closed: bool = False
        self._drain_called: bool = False

    def _match_pattern(self, pattern: str, subject: str) -> bool:
        """Match NATS subject patterns (* and >)."""
        if pattern == subject:
            return True
        # Convert NATS wildcard to regex
        regex_pattern = (
            "^" + pattern.replace(".", "\\.").replace("*", "[^.]+").replace(">", ".*") + "$"
        )
        return bool(re.match(regex_pattern, subject))

    def jetstream(self, **kwargs: Any) -> Any:
        mock_js = MagicMock()
        mock_js.publish = self.publish
        mock_js.subscribe = self.subscribe
        return mock_js

    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        reply: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not self.is_connected:
            raise RuntimeError("nats: connection closed")
        self.published_messages.append((subject, payload, headers, reply))

        msg = MockJetStreamMsg(
            subject=subject,
            data=payload,
            headers=headers,
            reply=reply,
            transport=self,
        )
        # Dispatch to matching subscribers
        for pattern, callbacks in list(self.subscriptions.items()):
            if self._match_pattern(pattern, subject):
                for cb in callbacks:
                    res = cb(msg)
                    if asyncio.iscoroutine(res):
                        await res

    async def request(
        self,
        subject: str,
        payload: bytes = b"",
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> MockJetStreamMsg:
        reply_inbox = f"_INBOX.{id(object())}"
        reply_future: asyncio.Future[MockJetStreamMsg] = asyncio.get_running_loop().create_future()

        async def reply_handler(msg: MockJetStreamMsg) -> None:
            if not reply_future.done():
                reply_future.set_result(msg)

        await self.subscribe(reply_inbox, reply_handler)
        try:
            await self.publish(subject, payload, reply=reply_inbox, headers=headers)
            return await asyncio.wait_for(reply_future, timeout=timeout)
        finally:
            self.unsubscribe(reply_inbox, reply_handler)

    async def subscribe(
        self,
        subject: str,
        cb: Callable[[MockJetStreamMsg], Any],
        queue: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if subject not in self.subscriptions:
            self.subscriptions[subject] = []
        self.subscriptions[subject].append(cb)

        class Subscription:
            def __init__(self, transport: MockNatsTransport, subj: str, callback: Any) -> None:
                self.transport = transport
                self.subject = subj
                self.callback = callback

            async def unsubscribe(self) -> None:
                self.transport.unsubscribe(self.subject, self.callback)

        return Subscription(self, subject, cb)

    def unsubscribe(self, subject: str, cb: Callable[[MockJetStreamMsg], Any]) -> None:
        if subject in self.subscriptions and cb in self.subscriptions[subject]:
            self.subscriptions[subject].remove(cb)
            if not self.subscriptions[subject]:
                del self.subscriptions[subject]

    async def drain(self) -> None:
        self._drain_called = True
        self.is_connected = False
        self.subscriptions.clear()

    async def close(self) -> None:
        self.is_closed = True
        self.is_connected = False
        self.subscriptions.clear()


@pytest.fixture
def mock_transport() -> MockNatsTransport:
    """Provide clean in-memory NATS transport for tests."""
    return MockNatsTransport()


@pytest_asyncio.fixture
async def e2e_service_factory() -> AsyncGenerator[Callable[..., CliffracerService]]:
    """Factory for spinning up isolated test services with clean lifecycle teardown."""
    created_services: list[CliffracerService] = []

    def _create(
        service_cls: type[CliffracerService] = CliffracerService,
        name: str = "test_e2e_svc",
        **config_kwargs: Any,
    ) -> CliffracerService:
        config = ServiceConfig(
            name=name,
            nats_url="nats://localhost:4222",
            auto_restart=False,
            request_timeout=5.0,
            **config_kwargs,
        )
        svc = service_cls(config)
        created_services.append(svc)
        return svc

    yield _create

    for svc in created_services:
        try:
            if hasattr(svc, "_running") and svc._running:
                await svc.stop()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _clean_test_context() -> Any:
    """Ensure correlation context is pristine before and after every E2E test."""
    CorrelationContext.clear()
    token = correlation_id_var.set(None)
    yield
    CorrelationContext.clear()
    correlation_id_var.reset(token)

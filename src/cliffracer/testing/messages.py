"""Mock message envelope and response abstractions for decoupled testing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cliffracer.core.validation import deserialize_payload


class MockMessage:
    """Mock NATS message envelope supporting RPC and event interaction."""

    def __init__(
        self,
        subject: str,
        data: bytes = b"",
        headers: dict[str, str] | None = None,
        reply: str | None = "_INBOX.test",
        *,
        metadata: Any = None,
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = headers if headers is not None else {}
        self.reply = reply
        self.metadata = metadata
        self.responded_data: bytes | None = None
        self.response_headers: dict[str, str] = {}
        self.acked = False
        self.nacked = False
        self.nak_delay: float | None = None
        self.terminated = False
        self.in_progress_called = False

    async def respond(self, data: bytes) -> None:
        """Capture response bytes and headers from the responder."""
        self.responded_data = data
        if self.headers:
            self.response_headers = dict(self.headers)

    async def ack(self) -> None:
        """Record JetStream acknowledgement."""
        self.acked = True

    async def nak(self, delay: float | None = None) -> None:
        """Record JetStream negative acknowledgement."""
        self.nacked = True
        self.nak_delay = delay

    async def term(self) -> None:
        """Record JetStream termination."""
        self.terminated = True

    async def in_progress(self) -> None:
        """Record in-progress heartbeat."""
        self.in_progress_called = True


@dataclass
class TestResponse:
    """Decoded RPC test response containing payload and execution metadata."""

    __test__ = False

    raw_data: bytes = b""
    data: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str | None = None

    @property
    def success(self) -> bool:
        """Whether the handler executed without reporting an error."""
        if isinstance(self.data, dict):
            if "success" in self.data:
                return bool(self.data["success"])
            return "error" not in self.data
        return True

    @property
    def error(self) -> str | None:
        """Error string returned by the handler, if any."""
        if isinstance(self.data, dict):
            return self.data.get("error")
        return None

    @property
    def result(self) -> Any:
        """Result payload returned by the handler."""
        if isinstance(self.data, dict) and "result" in self.data:
            return self.data["result"]
        return self.data

    @classmethod
    def from_mock_message(cls, msg: MockMessage) -> TestResponse:
        """Construct a TestResponse from an answered MockMessage."""
        raw = msg.responded_data or b""
        ct = msg.response_headers.get("Content-Type") or msg.headers.get("Content-Type")
        deserialized = deserialize_payload(raw, content_type=ct) if raw else None
        return cls(
            raw_data=raw,
            data=deserialized,
            headers=dict(msg.response_headers or msg.headers),
            content_type=ct,
        )

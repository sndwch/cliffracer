"""An in-memory JetStream context for the test harness.

A service configured for JetStream takes a different publish path from one that
is not: it refuses a subject no declared stream covers, and it publishes through
the JetStream context rather than the core connection. A harness that leaves the
context unset turns all of that off, so a service under test takes the core path
whatever its config says.

This stands in for the context, recording what was published so a test can
assert on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockPubAck:
    """The acknowledgement a JetStream publish returns."""

    stream: str = "MOCK"
    seq: int = 1
    domain: str | None = None
    duplicate: bool = False


@dataclass
class MockJetStreamContext:
    """In-memory stand-in for a JetStream context, recording each publish."""

    published: list[tuple[str, bytes, dict[str, str]]] = field(default_factory=list)
    stream_name: str = "MOCK"

    @property
    def published_subjects(self) -> list[str]:
        """The subject of each publish, in order."""
        return [subject for subject, _, _ in self.published]

    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        timeout: float | None = None,
        stream: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> MockPubAck:
        """Record the publish and answer with an acknowledgement."""
        self.published.append((subject, payload, dict(headers or {})))
        return MockPubAck(stream=stream or self.stream_name, seq=len(self.published))

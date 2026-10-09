"""Mock message envelope and response abstractions for decoupled testing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from nats.aio.msg import Msg
from nats.errors import Error, MsgAlreadyAckdError, NotJSMessageError

from cliffracer.core.validation import deserialize_payload


@dataclass
class MockJetStreamMetadata:
    """The delivery metadata a JetStream message carries.

    ``num_delivered`` is the one the dispatcher reads: it decides whether a
    failed delivery is redelivered or terminated, so a test of that policy has
    to be able to set it.

    ``sequence`` has the shape a real delivery's has, `nats.aio.msg.Msg.Metadata.SequencePair`
    (the consumer's and the stream's sequence numbers): the dead-letter record reads
    `sequence.stream` for `stream_sequence` and for the `Nats-Msg-Id` it publishes under.
    """

    num_delivered: int = 1
    stream: str = "MOCK"
    consumer: str = "mock-consumer"
    sequence: Msg.Metadata.SequencePair = field(
        default_factory=lambda: Msg.Metadata.SequencePair(consumer=1, stream=1)
    )


def refuse_a_reply_with_no_subject(msg: Any) -> None:
    """Raise what `nats.aio.msg.Msg.respond` raises when there is no reply subject.

    The rule lives here so there is ONE of it. A message with no reply subject
    is fire-and-forget, and the real `respond` refuses it -- so a double that
    records the reply instead lets a test assert a successful response that
    production would have refused.

    Hand-written doubles in this repository call this rather than each carrying
    the check: the same contract had three spellings and a dozen absences, and
    `tests/repo/test_every_message_double_refuses_a_reply_it_cannot_send.py`
    asserts that every double defining `respond` reaches it.

    `getattr` rather than `msg.reply`, because a double may not define the
    attribute at all; absent is the same answer as empty, which is what
    `Msg.respond`'s own `if not self.reply` says.
    """
    if not getattr(msg, "reply", None):
        raise Error("no reply subject available")


class MockMessage:
    """Mock NATS message envelope supporting RPC and event interaction."""

    def __init__(
        self,
        subject: str,
        data: bytes = b"",
        headers: Mapping[str, str] | None = None,
        reply: str | None = "_INBOX.test",
        *,
        metadata: Any = None,
    ) -> None:
        self.subject = subject
        self.data = data
        # Checked HERE, where the wrong argument was passed, rather than where
        # it is first used. `respond` copies the headers with `dict(...)`, so a
        # string arrives as "dictionary update sequence element #0 has length 1"
        # -- a message about neither the argument nor the call that supplied it,
        # raised from a method the caller did not think they were misusing.
        #
        # The position is the trap: `headers` is third and `reply` fourth, and a
        # reply subject is the thing a caller most often wants to pass, so three
        # positional arguments bind one to the other silently.
        # `Mapping`, not `dict`, and that is the ANNOTATION as well as the
        # check. `respond` copies with `dict(...)`, which takes any mapping, so
        # a narrower annotation would reject at type-check time what the code
        # accepts at run time -- and this module is exported, so the caller who
        # meets that contradiction is outside this tree, where `mypy src/` does
        # not look. The stored attribute is still a plain `dict`.
        if headers is not None and not isinstance(headers, Mapping):
            raise TypeError(
                f"MockMessage(headers=...) must be a mapping or None, not "
                f"{type(headers).__name__}: {headers!r}. `headers` is the THIRD "
                f"positional parameter and `reply` the fourth -- "
                f"MockMessage(subject, data, headers, reply) -- so pass a reply "
                f"subject by keyword: MockMessage(subject, data, reply=...)."
            )
        self.headers = dict(headers) if headers is not None else {}
        self.reply = reply
        self._metadata = metadata
        self.responded_data: bytes | None = None
        self.respond_calls = 0
        self.response_headers: dict[str, str] = {}
        self.acked = False
        self.nacked = False
        self.nak_delay: float | None = None
        self.terminated = False
        self.in_progress_called = False
        self._ackd = False

    @property
    def metadata(self) -> Any:
        """The delivery metadata the message was given, as `nats.aio.msg.Msg.metadata` answers.

        A message that was given none is a core message, and the real property raises
        `NotJSMessageError` for one; so does this. Code that reads it with a plain
        `getattr(msg, "metadata", None)` fails here as it fails against a real core message.
        """
        if self._metadata is None:
            raise NotJSMessageError
        return self._metadata

    @metadata.setter
    def metadata(self, value: Any) -> None:
        self._metadata = value

    @property
    def is_acked(self) -> bool:
        """Whether a terminal acknowledgement has been sent for this message."""
        return self._ackd

    def _check_reply(self) -> None:
        """Apply the acknowledgement rules `nats.aio.msg.Msg` applies.

        A message with no reply subject does not belong to a stream, and a
        terminal acknowledgement lands at most once.
        """
        if self.reply is None or self.reply == "":
            raise NotJSMessageError
        if self._ackd:
            raise MsgAlreadyAckdError(self)

    async def respond(self, data: bytes) -> None:
        """Capture response bytes and headers, refusing what the real Msg refuses.

        `nats.aio.msg.Msg.respond` raises when there is no reply subject, so a
        handler replying to a fire-and-forget message failed in production and
        recorded a successful reply here.

        Mirrors `Msg.respond`'s own check rather than reusing `_check_reply`:
        the real method tests `if not self.reply` and raises the generic
        `nats.errors.Error`, where `_check_reply` raises `NotJSMessageError` and
        also refuses an already-acknowledged message -- which `respond` permits.

        `respond_calls` counts attempts, including refused ones, and is
        incremented before the check. A caller asserting that respond was never
        called cannot use `responded_data`: every dispatcher path in this
        repository wraps `msg.respond` in `except Exception` and logs at debug,
        so a refused call leaves `responded_data` at None and looks identical to
        no call at all.
        """
        self.respond_calls += 1
        refuse_a_reply_with_no_subject(self)
        self.responded_data = data
        if self.headers:
            self.response_headers = dict(self.headers)

    async def ack(self) -> None:
        """Record JetStream acknowledgement."""
        self._check_reply()
        self.acked = True
        self._ackd = True

    async def nak(self, delay: float | None = None) -> None:
        """Record JetStream negative acknowledgement."""
        self._check_reply()
        self.nacked = True
        self.nak_delay = delay
        self._ackd = True

    async def term(self) -> None:
        """Record JetStream termination."""
        self._check_reply()
        self.terminated = True
        self._ackd = True

    async def in_progress(self) -> None:
        """Record in-progress heartbeat.

        An in-progress pulse is not terminal, so it repeats and does not
        acknowledge the message.
        """
        if self.reply is None or self.reply == "":
            raise NotJSMessageError
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
        """Whether the handler answered, and answered without an error.

        A handler that ran cleanly and replied with nothing reports False: an
        absent reply is a failure here, because a test asserting `success` is
        asserting that a reply came back.
        """
        if not self.raw_data and self.data is None:
            return False
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

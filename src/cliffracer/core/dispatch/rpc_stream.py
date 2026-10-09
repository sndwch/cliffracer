"""An RPC handler that streams its reply: the chunks, the caller that has gone, and the limits.

A handler written as an async generator, its return annotated `AsyncIterator[X]`, answers a request
that carries `Cliffracer-Stream: 1` with one message per item it yields, on the request's reply
subject, then one envelope that ends the stream. Each chunk is the item's own dump, headed
`Cliffracer-Stream-Seq` with its index from 0; the envelope is the usual one, with `"items"` and
the header `Cliffracer-Stream-End` giving the count, so a caller sees any chunk the broker dropped.

Each chunk is published with a reply subject of the service's own, held by one plain subscription
on the same connection. When the caller has gone, the broker answers the next chunk there with a
503 (it sends that 503 to the publishing connection only, on a subscription outside a queue group),
and the stream stops.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..messages import with_correlation_id
from ..service_config import ServiceConfig
from ..subjects import inbox_subject
from ..validation import serialize_payload

#: The request header a caller sets to ask for a streamed reply.
STREAM_HEADER = "Cliffracer-Stream"
#: A chunk's index in the stream, from 0.
SEQ_HEADER = "Cliffracer-Stream-Seq"
#: The number of chunks the stream sent, on the envelope that ends it.
END_HEADER = "Cliffracer-Stream-End"
#: What the fire-and-forget path logs for a handler that streams, which it does not run.
NOT_RUN_ASYNC = (
    "Async request {} not run: it streams its reply, and a fire-and-forget call has nobody to "
    "stream to"
)


def mismatch(
    streams: bool, headers: Mapping[str, Any], handler_name: str, correlation_id: Any
) -> dict[str, Any] | None:
    """The answer to a request whose `Cliffracer-Stream` header does not match the handler, or
    None when it matches."""
    asked = any(k.lower() == STREAM_HEADER.lower() and v == "1" for k, v in headers.items())
    if asked == streams:
        return None
    said = (
        f"{handler_name} streams its reply; call it as a stream, with {STREAM_HEADER}: 1"
        if streams
        else f"{handler_name} does not stream its reply; call it without {STREAM_HEADER}"
    )
    return {
        "success": False,
        "error": "validation failed",
        "code": "validation_failed",
        "details": [{"loc": ["__root__"], "msg": said, "type": "stream_mismatch"}],
        "timestamp": datetime.now(UTC).isoformat(),
        "correlation_id": correlation_id,
    }


@dataclass(frozen=True)
class StreamEnded:
    """How a streaming handler's call ended: the chunks it sent, and, when it ended before the
    handler did, the envelope that says why (`refusal`) or that nobody is listening (`gone`)."""

    items: int
    refusal: dict[str, Any] | None = None
    gone: bool = False

    @property
    def complete(self) -> bool:
        return self.refusal is None and not self.gone


class _LimitReached(Exception):  # noqa: N818 - a stop, not a fault of the handler
    def __init__(self, kind: str, limit: int) -> None:
        super().__init__(f"the stream reached its {kind} limit, {limit}")
        self.kind = kind
        self.limit = limit


class Streams:
    """A service's streamed replies: the one subscription that hears a gone caller, made at the
    first stream on the connection the chunks are published on, outside a queue group (the broker
    sends its 503 nowhere else), and the streams that have one."""

    def __init__(self, connection: Callable[[], Any], config: ServiceConfig, logger: Any) -> None:
        self._connection = connection
        self._config = config
        self._logger = logger
        # Under the service's inbox prefix, as a client's own inboxes are, which is where a
        # service's broker grant lets it subscribe.
        self._root = uuid.uuid4().hex
        self._listening = False
        # The streams sending now, and those of them whose caller has gone. A 503 is heard only
        # for a stream still sending, so one that arrives after its stream ended leaves nothing
        # behind, and both sets hold at most the streams in flight.
        self._active: set[str] = set()
        self._gone: set[str] = set()

    def open(self, adapter: Any, reply_format: str, handler_name: str) -> StreamedRequest:
        return StreamedRequest(self, adapter, reply_format, handler_name)

    async def _back_subject(self, stream_id: str) -> str:
        prefix = self._config.nats_inbox_prefix
        if not self._listening:
            await self._connection().subscribe(
                inbox_subject(prefix, self._root, "*"), cb=self._heard
            )
            self._listening = True
        return inbox_subject(prefix, self._root, stream_id)

    async def _heard(self, msg: Any) -> None:
        stream_id = msg.subject.rsplit(".", 1)[-1]
        if (
            stream_id in self._active
            and (getattr(msg, "headers", None) or {}).get("Status") == "503"
        ):
            self._gone.add(stream_id)


@dataclass
class StreamedRequest:
    """One request a streaming handler answers: the chunks it has sent, and how it ends."""

    streams: Streams
    adapter: Any
    reply_format: str
    handler_name: str
    sent: int = 0
    _size: int = field(default=0, repr=False)

    def ending(self, envelope: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        """The envelope that ends the stream, with the count in its body and its header."""
        return {**envelope, "items": self.sent}, {END_HEADER: str(self.sent)}

    async def send(self, items: AsyncIterator[Any], reply: str, correlation_id: Any) -> StreamEnded:
        """Publish each item `items` yields as a chunk to `reply`, until it ends, a limit is
        reached or the caller is gone, and close the generator whatever ends it."""
        streams = self.streams
        stream_id = uuid.uuid4().hex
        back = await streams._back_subject(stream_id)
        streams._active.add(stream_id)
        try:
            async for item in items:
                await self._publish(item, reply, back, correlation_id)
                if stream_id in streams._gone:
                    streams._logger.info(
                        f"{self.handler_name} stopped: the caller is gone after {self.sent} items "
                        f"(correlation_id: {correlation_id})"
                    )
                    return StreamEnded(self.sent, gone=True)
        except _LimitReached as reached:
            streams._logger.warning(f"{self.handler_name} stopped: {reached}")
            return StreamEnded(self.sent, refusal=self._refusal(reached, correlation_id))
        finally:
            streams._active.discard(stream_id)
            streams._gone.discard(stream_id)
            aclose = getattr(items, "aclose", None)
            if aclose is not None:
                await aclose()
        return StreamEnded(self.sent)

    async def _publish(self, item: Any, reply: str, back: str, correlation_id: Any) -> None:
        dumped = self.adapter.dump_python(
            self.adapter.validate_python(with_correlation_id(item, correlation_id)), mode="json"
        )
        try:
            body, content_type = serialize_payload(dumped, format=self.reply_format)
        except ImportError:
            body, content_type = serialize_payload(dumped, format="json")
        config = self.streams._config
        if config.max_stream_items is not None and self.sent >= config.max_stream_items:
            raise _LimitReached("items", config.max_stream_items)
        if config.max_stream_bytes is not None and self._size + len(body) > config.max_stream_bytes:
            raise _LimitReached("bytes", config.max_stream_bytes)
        headers = {"Content-Type": content_type, SEQ_HEADER: str(self.sent)}
        if isinstance(correlation_id, str) and correlation_id:
            headers["X-Correlation-ID"] = correlation_id
        await self.streams._connection().publish(reply, body, headers=headers, reply=back)
        self.sent += 1
        self._size += len(body)

    def _refusal(self, reached: _LimitReached, correlation_id: Any) -> dict[str, Any]:
        return {
            "success": False,
            "error": f"refused: {self.handler_name}: {reached}",
            "code": "refused",
            "limit": {reached.kind: reached.limit},
            "timestamp": datetime.now(UTC).isoformat(),
            "correlation_id": correlation_id,
        }

"""The caller's side of a streamed reply: open the stream, then read each item and the envelope.

A streaming handler answers a request carrying `Cliffracer-Stream: 1` with one message per item,
headed `Cliffracer-Stream-Seq` (its index from 0), then one envelope headed
`Cliffracer-Stream-End` (the number of items sent). The caller subscribes an inbox of its own
before it sends, so no item can arrive before anyone listens, and unsubscribes it however the
reading ends, which is how the service learns it has gone.

The generated client's `_stream` and the service's `stream_rpc` both read through here, so a
caller gets the same items and the same exceptions whichever it called through.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from nats.errors import Error as NatsError
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from .dispatch.rpc_stream import END_HEADER, SEQ_HEADER, STREAM_HEADER
from .exceptions import (
    RpcError,
    RpcNoRespondersError,
    RpcServerError,
    RpcStreamGapError,
    RpcTimeoutError,
)
from .validation import deserialize_payload

__all__ = ["STREAM_HEADER", "OpenStream", "open_stream", "read_stream"]


@dataclass
class OpenStream:
    """A stream's reply inbox: the subscription, and the messages it has received in order."""

    sub: Any
    arrived: asyncio.Queue[Any]


async def open_stream(
    nc: Any, subject: str, payload: bytes, headers: Mapping[str, str]
) -> OpenStream:
    """Subscribe a new inbox on `nc` and send the request with it as the reply subject. The SUB
    is written before the PUB on one connection, so the broker holds the inbox's interest before
    the request reaches any service."""
    arrived: asyncio.Queue[Any] = asyncio.Queue()

    async def arrive(msg: Any) -> None:
        arrived.put_nowait(msg)

    sub = await nc.subscribe(nc.new_inbox(), cb=arrive)
    try:
        await nc.publish(
            subject, payload, reply=sub.subject, headers={**headers, STREAM_HEADER: "1"}
        )
    except BaseException:
        await _leave(sub)
        raise
    return OpenStream(sub, arrived)


async def read_stream(
    opened: OpenStream,
    subject: str,
    *,
    timeout: float | None,
    idle_timeout: float | None = None,
    item_type: Any = None,
    fallback_format: str = "json",
    raise_for_envelope: Callable[[dict[str, Any], str], None],
) -> AsyncGenerator[Any]:
    """Yield each item that arrives on the stream's inbox, validated against `item_type` when one
    is given, until the envelope that ends the stream; raise what that envelope says when it is an
    error.

    `timeout` bounds the whole stream, and each wait for the next message within it (None: no
    bound of its own);
    `idle_timeout`, when given, bounds each wait on its own as well. An exception
    raised here carries `items`, the number of items yielded before it. The subscription is
    unsubscribed however this ends: the end, an error (a gap, a mismatched item, a timeout),
    `break`, cancellation or `aclose()`.
    """
    adapter = None if item_type is None else TypeAdapter(item_type)
    deadline = math.inf if timeout is None else time.monotonic() + timeout
    received = 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            idle = idle_timeout is not None and idle_timeout < remaining
            try:
                if remaining <= 0:
                    raise TimeoutError
                msg = await asyncio.wait_for(
                    opened.arrived.get(),
                    idle_timeout if idle else (None if math.isinf(remaining) else remaining),
                )
            except TimeoutError as exc:
                which = (
                    f"sent nothing for {idle_timeout}s"
                    if idle
                    else f"did not end within {timeout}s"
                )
                raise _counted(
                    RpcTimeoutError(f"the stream from {subject} {which}, after {received} items"),
                    received,
                ) from exc
            headers = msg.headers or {}
            if headers.get("Status") == "503":
                raise _counted(
                    RpcNoRespondersError(
                        f"nothing is subscribed to {subject}; is the service running?"
                    ),
                    received,
                )
            if END_HEADER in headers:
                _end(
                    msg, headers[END_HEADER], received, subject, fallback_format, raise_for_envelope
                )
                return
            if SEQ_HEADER not in headers:
                raise _counted(
                    RpcServerError(
                        f"{subject} sent a message that is neither an item nor the end of its "
                        f"stream: {_preview(msg.data)}"
                    ),
                    received,
                )
            if headers[SEQ_HEADER] != str(received):
                got = headers[SEQ_HEADER]
                raise RpcStreamGapError(
                    subject,
                    expected=received,
                    got=int(got) if got.isdigit() else got,
                    items=received,
                )
            item = _decode(msg, subject, fallback_format, received)
            if adapter is not None:
                try:
                    item = adapter.validate_python(item)
                except PydanticValidationError as exc:
                    raise _counted(
                        RpcServerError(
                            f"{subject} sent item {received}, which does not match its declared "
                            f"type: {exc}"
                        ),
                        received,
                    ) from exc
            received += 1
            yield item
    finally:
        await _leave(opened.sub)


def _end(
    msg: Any,
    count: str,
    received: int,
    subject: str,
    fallback_format: str,
    raise_for_envelope: Callable[[dict[str, Any], str], None],
) -> None:
    """Read the envelope that ends the stream: a count other than the items received is a gap,
    an error envelope raises what it says, and success returns."""
    envelope = _decode(msg, subject, fallback_format, received)
    if not isinstance(envelope, dict):
        raise _counted(
            RpcServerError(
                f"{subject} ended its stream with {type(envelope).__name__}, not an object: "
                f"{_preview(msg.data)}"
            ),
            received,
        )
    if count != str(received):
        raise RpcStreamGapError(
            subject, expected=int(count) if count.isdigit() else count, got=received, items=received
        )
    try:
        raise_for_envelope(envelope, subject)
    except RpcError as exc:
        exc.items = received
        raise
    if envelope.get("success") is not True:
        raise _counted(
            RpcServerError(
                f"{subject} ended its stream without success and without saying why: "
                f"{_preview(msg.data)}"
            ),
            received,
        )


def _decode(msg: Any, subject: str, fallback_format: str, received: int) -> Any:
    content_type = next(
        (str(v) for k, v in (msg.headers or {}).items() if str(k).lower() == "content-type"), None
    )
    try:
        return deserialize_payload(
            msg.data, content_type=content_type, fallback_format=fallback_format
        )
    except Exception as exc:
        raise _counted(
            RpcServerError(
                f"{subject} sent something this caller cannot read: {_preview(msg.data)}"
            ),
            received,
        ) from exc


def _counted(exc: RpcError, received: int) -> RpcError:
    exc.items = received
    return exc


async def _leave(sub: Any) -> None:
    """Unsubscribe; one whose connection has closed is already gone."""
    try:
        await sub.unsubscribe()
    except NatsError:
        pass


def _preview(payload: object, limit: int = 120) -> str:
    """A short, printable look at a payload, for a message a human reads. Typed `object` because
    the failures this appears in are exactly the ones where the payload is not what it should be."""
    if not isinstance(payload, bytes | bytearray):
        return repr(payload)[:limit]
    text = bytes(payload[:limit]).decode("utf-8", errors="replace")
    return f"{text!r}{'...' if len(payload) > limit else ''}"

"""Reading the dead-letter stream, and nothing else.

The stream's own message-get API: no consumer is created, nothing is acknowledged and no cursor
moves, so reading cannot disturb the stream or leave anything behind on the broker.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from nats.js.errors import APIError, NotFoundError

from cliffracer_dlq.records import DeadLetter


class StreamChoiceError(Exception):
    """No stream holds the subject, or the named stream does not exist."""


#: The one request to the broker that is not a read of a stream: it lists stream names by subject.
STREAM_NAMES_API = "$JS.API.STREAM.NAMES"


async def stream_names(nc: Any, subject: str, timeout: float) -> list[str]:
    """The names of the streams that hold `subject`, from one `STREAM.NAMES` request.

    nats-py's own `find_stream_name_by_subject` makes this request and keeps the first name, so it
    cannot say that a wildcard matches several streams. The broker refuses two streams that share
    a literal subject, so more than one name means `subject` is a wildcard.
    """
    reply = await nc.request(
        STREAM_NAMES_API, json.dumps({"subject": subject}).encode(), timeout=timeout
    )
    body = json.loads(reply.data)
    if "error" in body:
        APIError.from_error(body["error"])
    return [str(name) for name in body.get("streams") or []]


async def resolve_stream(broker: Any, stream: str | None, subject: str) -> str:
    """The stream to read: the one named, or the only one the broker says holds `subject`.

    Without `--stream`, a subject that no stream holds, or a wildcard that several do, is refused
    naming what was found: reading one of several would count only part of the dead letters.
    """
    if stream is not None:
        return stream
    names = await broker.stream_names(subject)
    if not names:
        raise StreamChoiceError(
            f"no stream holds the subject {subject!r}; name the stream with --stream or the "
            f"subject with --subject"
        )
    if len(names) > 1:
        raise StreamChoiceError(
            f"{len(names)} streams hold the subject {subject!r}: {', '.join(sorted(names))}; "
            f"choose one with --stream"
        )
    return str(names[0])


async def ensure_stream(jsm: Any, stream: str) -> None:
    """Refuse a stream the broker does not hold, so a missing stream is not mistaken for a missing message."""
    try:
        await jsm.stream_info(stream)
    except NotFoundError as exc:
        raise StreamChoiceError(f"the broker holds no stream named {stream!r}") from exc


async def read(jsm: Any, stream: str, subject: str) -> AsyncIterator[DeadLetter]:
    """Every message of `stream` on `subject`, oldest first."""
    state = (await jsm.stream_info(stream)).state
    if not state.messages:
        return
    sequence, last = state.first_seq, state.last_seq
    while sequence <= last:
        try:
            message = await jsm.get_msg(stream, seq=sequence, subject=subject, next=True)
        except NotFoundError:
            return
        yield DeadLetter.from_message(message)
        sequence = int(message.seq) + 1


async def read_one(jsm: Any, stream: str, sequence: int) -> DeadLetter | None:
    """The message at `sequence` in `stream`, or None when the stream holds none there."""
    try:
        message = await jsm.get_msg(stream, seq=sequence)
    except NotFoundError:
        return None
    return DeadLetter.from_message(message)

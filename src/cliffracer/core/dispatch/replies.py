"""The reply an RPC request gets: its headers and how it is sent.

A reply carries the headers the service decided on and no others: `Msg.respond` would publish the
request's own headers on it, so they are replaced first.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def reply_headers(content_type: str | None, correlation_id: Any) -> dict[str, str]:
    """The headers an RPC reply carries: the ones the service decided on, and no others.

    `Msg.respond` publishes the inbound message's own headers on the reply, so a reply that
    is sent as it is carries whatever the caller put on its request, including a correlation id
    the service refused. The reply carries its content type and the correlation id the service
    used; everything else about the outcome is in the envelope.
    """
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    if isinstance(correlation_id, str) and correlation_id:
        headers["X-Correlation-ID"] = correlation_id
    return headers


async def answer(
    msg: Any,
    data: bytes,
    *,
    content_type: str | None,
    correlation_id: Any = None,
    extra_headers: Mapping[str, str] | None = None,
) -> None:
    """Reply to `msg` with `data` and exactly the headers `reply_headers` names, and
    `extra_headers` (the count on the envelope that ends a stream)."""
    if hasattr(msg, "headers"):
        msg.headers = {**reply_headers(content_type, correlation_id), **(extra_headers or {})}
    await msg.respond(data)

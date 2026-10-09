"""Call a running service without a generated client: `call` for one reply, `stream` for a
streamed one.

Both take the connection to call over, the service's name, the method's and its arguments as a
mapping of JSON-ready values, and send the request as `call_rpc` and `stream_rpc` do: on the
method's subject (with its namespace and environment prefix), labelled
`Content-Type: application/json`, with the caller's correlation id and the budget
(`Cliffracer-Timeout-Ms`) it waits, cut to what an enclosing request has left.

What an untyped call gives up, against a generated client's method: the arguments go out as given,
so a model is not written in the form its service reads (one is refused here, by name), and a
value the wire would lose is not refused; the service's signatures are not compared, so a call to
a method that changed is answered as the service now reads it, never refused as out of date; and
the result is the decoded JSON, not validated against a return type.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import uuid
from collections.abc import AsyncGenerator, Mapping
from typing import Any, NamedTuple

from nats.errors import Error as NatsError
from nats.errors import NoRespondersError
from nats.errors import TimeoutError as NatsTimeoutError
from pydantic import BaseModel

from .core.correlation import CorrelationContext
from .core.deadline import TIMEOUT_HEADER, header_value, outbound_timeout, refuse_a_duration
from .core.decorators import _unusable_subject_reason
from .core.discovery import HandlerDiscovery
from .core.exceptions import (
    RpcClientError,
    RpcNoRespondersError,
    RpcTimeoutError,
    raise_for_error_envelope,
)
from .core.nats_errors import rpc_error_for
from .core.rpc_calls import reply_result
from .core.stream_reader import open_stream, read_stream
from .core.validation import CONTENT_TYPE_JSON

__all__ = ["Prepared", "call", "prepare", "stream"]

#: The seconds a call waits by default, as `ServiceClient` and `request_timeout` do.
DEFAULT_TIMEOUT = 30.0


async def call(
    nc: Any,
    service: str,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    namespace: str | None = None,
    subject_prefix: str | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Call `service`'s `method` over `nc` and return the reply's `result`.

    An error the service answers is raised as `call_rpc` raises it (`RpcValidationError`,
    `RpcRefusedError`, `RpcDeadlineExceededError`, `RpcServerError`, ...); a call nothing answers
    raises `RpcNoRespondersError`, and one not answered within `timeout` raises `RpcTimeoutError`.
    `subject_prefix` defaults to `CLIFFRACER_SUBJECT_PREFIX`; `""` calls the unprefixed subject.
    `timeout=None` waits with no bound and sends no budget. The request is `prepare`'s.
    """
    subject, sent, payload, wait = prepare(
        service,
        method,
        params,
        namespace=namespace,
        subject_prefix=subject_prefix,
        timeout=timeout,
        headers=headers,
    )
    try:
        response = await nc.request(subject, payload, timeout=wait, headers=sent)
    except NatsTimeoutError as exc:
        raise RpcTimeoutError(f"{subject} did not answer within {round(wait or 0, 3)}s") from exc
    except NoRespondersError as exc:
        raise RpcNoRespondersError(
            f"nothing is subscribed to {subject}; is {service} running?"
        ) from exc
    except NatsError as exc:
        raise rpc_error_for(exc, subject, awaiting_reply=True) from exc
    return reply_result(response, subject, "json")


def stream(
    nc: Any,
    service: str,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    namespace: str | None = None,
    subject_prefix: str | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    idle_timeout: float | None = None,
    headers: Mapping[str, str] | None = None,
) -> AsyncGenerator[Any]:
    """Call `service`'s `method`, which streams its reply, over `nc`, and yield each item as it
    arrives, for `async for`.

    `timeout` bounds the whole stream, and `idle_timeout`, when given, each wait for the next
    message: a stream that sends nothing for that long raises `RpcTimeoutError` naming it. An
    error the stream ends with is raised after the items before it, carrying `items`. Arguments
    are checked here, when it is called, so a refused one is raised before anything is iterated.
    """
    if idle_timeout is not None:
        refuse_a_duration("idle_timeout", idle_timeout)
    prepared = prepare(
        service,
        method,
        params,
        namespace=namespace,
        subject_prefix=subject_prefix,
        timeout=timeout,
        headers=headers,
    )
    return _stream(nc, *prepared, idle_timeout)


async def _stream(
    nc: Any,
    subject: str,
    sent: dict[str, str],
    payload: bytes,
    wait: float | None,
    idle_timeout: float | None,
) -> AsyncGenerator[Any]:
    try:
        opened = await open_stream(nc, subject, payload, sent)
    except NatsError as exc:
        raise rpc_error_for(exc, subject, awaiting_reply=True) from exc
    # Held under aclosing, so closing this generator closes the reader and its reply inbox at once.
    reader = read_stream(
        opened,
        subject,
        timeout=wait,
        idle_timeout=idle_timeout,
        raise_for_envelope=raise_for_error_envelope,
    )
    async with contextlib.aclosing(reader) as items:
        async for item in items:
            yield item


class Prepared(NamedTuple):
    """A call as it will be sent: its subject, its headers and its body, and how long its caller
    waits for the reply (None for no bound)."""

    subject: str
    headers: dict[str, str]
    payload: bytes
    timeout: float | None


def prepare(
    service: str,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    namespace: str | None = None,
    subject_prefix: str | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
) -> Prepared:
    """The request `call` and `stream` send, built and checked without sending it: what a dry run
    shows. Each argument is checked here, so a refused one raises before anything is sent.

    `timeout=None` sets no bound of the caller's own: no wait limit and no budget header, unless
    an enclosing request has a deadline, whose remainder is then the wait and the budget.
    """
    if timeout is not None:
        refuse_a_duration("timeout", timeout)
    arguments = dict(params or {})
    _refuse_a_model(arguments, "params")
    subject = HandlerDiscovery.call_subject(
        service,
        "rpc",
        method,
        namespace=namespace,
        subject_prefix=(
            os.environ.get("CLIFFRACER_SUBJECT_PREFIX") or None
            if subject_prefix is None
            else subject_prefix or None
        ),
    )
    reason = _unusable_subject_reason(subject)
    if reason is not None:
        # As `call_rpc` refuses it: a subject the server cannot parse closes the caller's whole
        # connection, and one with an empty token is sent and answered by nobody.
        raise ValueError(f"Invalid RPC subject {subject!r}: {reason}")
    try:
        payload = json.dumps(arguments).encode()
    except (TypeError, ValueError) as exc:
        raise RpcClientError(f"an argument to {subject} cannot be encoded: {exc}") from exc
    left = outbound_timeout(math.inf if timeout is None else timeout, subject)
    wait = None if math.isinf(left) else left
    given = dict(headers or {})
    cid = (
        CorrelationContext.extract_from_headers(given)
        or CorrelationContext.ambient_for_send()
        or uuid.uuid4().hex
    )
    replaced = {"x-correlation-id", "correlation_id", "content-type"}
    sent = {name: value for name, value in given.items() if name.lower() not in replaced}
    sent.update({"Content-Type": CONTENT_TYPE_JSON, "X-Correlation-ID": cid, "correlation_id": cid})
    # A budget the caller set is sent as given, as `cliffracer call` sends one given with --header;
    # with no bound at all, none is sent and the service applies its own.
    if wait is not None and not any(name.lower() == TIMEOUT_HEADER.lower() for name in sent):
        sent[TIMEOUT_HEADER] = header_value(wait)
    return Prepared(subject, sent, payload, wait)


def _refuse_a_model(value: Any, where: str) -> None:
    """Refuse a pydantic model anywhere in the arguments: which form of it the service reads is
    the generated client's choice to make, and an untyped call cannot make it."""
    if isinstance(value, BaseModel):
        raise RpcClientError(
            f"{where} is a {type(value).__name__} model; an untyped call sends JSON-ready values "
            "only. Call the method through its generated client, which writes a model in the form "
            "the service reads, or pass the model's dump."
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            _refuse_a_model(item, f"{where}[{key!r}]")
    elif isinstance(value, list | tuple | set | frozenset):
        for index, item in enumerate(value):
            _refuse_a_model(item, f"{where}[{index}]")

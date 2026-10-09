"""The outbound RPC calls a service makes: the bodies of `CliffracerService.call_rpc` and
`stream_rpc`.

Each function takes the service as `self` and is called by the method of the same name on
`CliffracerService`, which documents it.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Mapping
from typing import TYPE_CHECKING, Any

from nats.errors import Error as NatsError
from nats.errors import NoRespondersError
from nats.errors import TimeoutError as NatsTimeoutError

from .correlation import CorrelationContext
from .deadline import TIMEOUT_HEADER, header_value, outbound_timeout
from .decorators import _unusable_subject_reason
from .discovery import HandlerDiscovery
from .exceptions import (
    RpcNoRespondersError,
    RpcServerError,
    RPCTimeoutError,
    raise_for_error_envelope,
)
from .nats_errors import rpc_error_for
from .stream_reader import OpenStream, _preview, open_stream, read_stream
from .validation import deserialize_payload, serialize_payload, wire_models

if TYPE_CHECKING:
    from .service import CliffracerService


def reply_content_type(response: Any) -> str | None:
    """A reply's declared content type, matched case-insensitively."""
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            return str(value)
    return None


def read_reply(response: Any, subject: str, fallback_format: str) -> dict[str, Any]:
    """An RPC reply decoded by its declared content type (`fallback_format` when it declares
    none), as an object, or an `RpcServerError` saying what came back.

    A reply that cannot be decoded, or that decodes to anything but an object, is the remote
    breaking the protocol, so it is raised as the remote's fault, with a prefix of the payload: a
    reply nobody can parse cannot be diagnosed without a sight of it.
    """
    try:
        data = deserialize_payload(
            response.data,
            content_type=reply_content_type(response),
            fallback_format=fallback_format,
        )
    except Exception as exc:
        raise RpcServerError(
            f"{subject} answered with something this caller cannot read: {_preview(response.data)}"
        ) from exc
    if not isinstance(data, dict):
        raise RpcServerError(
            f"{subject} answered with {type(data).__name__}, not an object: "
            f"{_preview(response.data)}"
        )
    return data


def require_success(data: dict[str, Any], subject: str) -> None:
    """Return if a reply with no error envelope says it succeeded, else raise `RpcServerError`.

    A reply must carry the `success` key: one that does not is the remote breaking the protocol.
    One that has it and says it failed, with no `error` for `raise_for_error_envelope` to read, is
    a failing responder that left out why.
    """
    if data.get("success") is True:
        return
    if "success" not in data:
        raise RpcServerError(f"protocol error: reply from {subject} carries no success key")
    raise RpcServerError(
        f"{subject} failed without saying why: the reply has success="
        f"{data['success']!r} and no error"
    )


def reply_result(response: Any, subject: str, fallback_format: str) -> Any:
    """The `result` of an RPC reply, or the error it carries, raised.

    The reply is read as `read_reply` reads it, an error envelope is raised as
    `raise_for_error_envelope` raises it, and a reply that does not say it succeeded is raised as
    `require_success` raises it: the reading a generated client applies too.
    """
    data = read_reply(response, subject, fallback_format)
    raise_for_error_envelope(data, subject)
    require_success(data, subject)
    return data.get("result")


async def call_rpc(
    self: CliffracerService,
    service: str,
    method: str,
    /,
    *,
    namespace: str | None = None,
    **kwargs: Any,
) -> Any:
    """`CliffracerService.call_rpc`."""
    subject = HandlerDiscovery.outbound_subject(
        self.config, service, "rpc", method, namespace=namespace
    )
    reason = _unusable_subject_reason(subject)
    if reason is not None:
        raise ValueError(f"Invalid RPC subject {subject!r}: {reason}")
    nc = self._require_connection(subject, call=True)

    kwargs["correlation_id"] = CorrelationContext.get_or_create_id(kwargs.get("correlation_id"))

    correlation_id = kwargs["correlation_id"]
    self.logger.info(f"Calling RPC {service}.{method} with correlation_id: {correlation_id}")

    request_data, content_type = serialize_payload(
        wire_models(kwargs), format=self.config.serialization_format
    )
    timeout = outbound_timeout(self.config.request_timeout, f"RPC {service}.{method}")
    ctx = self.container.dispatcher._send_context("call_rpc", subject, kwargs, correlation_id)
    ctx.headers["Content-Type"] = content_type
    ctx.headers[TIMEOUT_HEADER] = header_value(timeout)

    async def _send() -> Any:
        try:
            response = await nc.request(
                subject,
                request_data,
                timeout=timeout,
                headers=dict(ctx.headers),
            )

            return reply_result(response, subject, self.config.serialization_format)

        except NatsTimeoutError as e:
            self.logger.error(
                f"RPC timeout calling {service}.{method} (correlation_id: {correlation_id})"
            )
            raise RPCTimeoutError(f"RPC timeout calling {service}.{method}") from e
        except NoRespondersError as e:
            self.logger.error(
                f"No responders for RPC {service}.{method} on {subject} "
                f"(correlation_id: {correlation_id})"
            )
            raise RpcNoRespondersError(
                f"nothing is subscribed to {subject}; is {service} running?"
            ) from e
        except NatsError as e:
            self.logger.error(
                f"{type(e).__name__} during RPC {service}.{method} on {subject} "
                f"(correlation_id: {correlation_id})"
            )
            raise rpc_error_for(e, subject, awaiting_reply=True) from e

    return await self.container.dispatcher._run_send_hooks(ctx, _send)


async def stream_rpc(
    self: CliffracerService,
    service: str,
    method: str,
    /,
    *,
    namespace: str | None = None,
    **kwargs: Any,
) -> AsyncGenerator[Any]:
    """`CliffracerService.stream_rpc`."""
    subject = HandlerDiscovery.outbound_subject(
        self.config, service, "rpc", method, namespace=namespace
    )
    reason = _unusable_subject_reason(subject)
    if reason is not None:
        raise ValueError(f"Invalid RPC subject {subject!r}: {reason}")
    nc = self._require_connection(subject, call=True)

    kwargs["correlation_id"] = CorrelationContext.get_or_create_id(kwargs.get("correlation_id"))
    correlation_id = kwargs["correlation_id"]
    self.logger.info(f"Streaming RPC {service}.{method} with correlation_id: {correlation_id}")

    request_data, content_type = serialize_payload(
        wire_models(kwargs), format=self.config.serialization_format
    )
    timeout = outbound_timeout(self.config.request_timeout, f"RPC {service}.{method}")
    ctx = self.container.dispatcher._send_context("stream_rpc", subject, kwargs, correlation_id)
    ctx.headers["Content-Type"] = content_type
    ctx.headers[TIMEOUT_HEADER] = header_value(timeout)

    opened: list[OpenStream] = []

    async def _open() -> None:
        try:
            opened.append(await open_stream(nc, subject, request_data, dict(ctx.headers)))
        except NatsError as e:
            raise rpc_error_for(e, subject, awaiting_reply=True) from e

    await self.container.dispatcher._run_send_hooks(ctx, _open)
    # Held under aclosing, so closing this generator closes the reader and its reply inbox at once.
    reader = read_stream(
        opened[0],
        subject,
        timeout=timeout,
        fallback_format=self.config.serialization_format,
        raise_for_envelope=raise_for_error_envelope,
    )
    async with contextlib.aclosing(reader) as items:
        async for item in items:
            yield item

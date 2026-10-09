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
from .exceptions import RpcNoRespondersError, RPCTimeoutError, raise_for_error_envelope
from .nats_errors import rpc_error_for
from .stream_reader import OpenStream, open_stream, read_stream
from .validation import deserialize_payload, serialize_payload, wire_models

if TYPE_CHECKING:
    from .service import CliffracerService


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

            resp_h = getattr(response, "headers", None)
            reply_headers = dict(resp_h) if isinstance(resp_h, Mapping) else {}
            reply_ct = None
            for k, v in reply_headers.items():
                if k.lower() == "content-type":
                    reply_ct = v
                    break

            response_data = deserialize_payload(
                response.data,
                content_type=reply_ct,
                fallback_format=self.config.serialization_format,
            )

            raise_for_error_envelope(response_data, subject)

            return response_data.get("result")

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

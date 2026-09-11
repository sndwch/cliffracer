"""Dead-letter queue evaluation, formatting, and publishing."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from loguru import logger as global_logger

from ..correlation import CorrelationContext
from ..jetstream import StreamDeclarationError, subject_covered_by
from ..service_config import ServiceConfig
from ..validation import serialize_payload


class DeadLetterPublisher:
    """Evaluates DLQ subject templates and publishes error/diagnostic messages.

    Invariants:
    - Never swallows stream declaration errors when JetStream is active.
    - Injects correlation_id into headers and payload if present or generatable.
    - Respects configured serialization format when converting diagnostic payloads.
    - Genuine transport publisher with zero circular container dependencies.
    """

    def __init__(
        self,
        config: ServiceConfig,
        connection_provider: Callable[[], Any],
        logger: Any = None,
        service: Any = None,
    ) -> None:
        self.config = config
        self.connection_provider = connection_provider
        self.logger = logger or global_logger.bind(service=config.name)
        self.service = service

    @property
    def nc(self) -> Any:
        conn = self.connection_provider()
        return getattr(conn, "nc", None)

    @property
    def js(self) -> Any:
        conn = self.connection_provider()
        return getattr(conn, "js", None)

    @property
    def _jetstream_active(self) -> bool:
        conn = self.connection_provider()
        return getattr(conn, "jetstream_active", False)

    def format_dlq_subject(self) -> str:
        """Format configured dead-letter subject template."""
        return self.config.dlq_subject.format(
            service=self.config.name,
            namespace=self.config.namespace or "",
        )

    async def publish_dlq(
        self,
        subject: str,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Publish an unroutable or error diagnostic message to the dead-letter queue."""
        headers = dict(headers or {})
        if not isinstance(payload, bytes):
            data = dict(kwargs)
            if payload is not None:
                data["payload"] = payload
            cid = (
                data.get("correlation_id")
                or CorrelationContext.get()
                or CorrelationContext.get_or_create_id()
            )
            if cid:
                data["correlation_id"] = cid
                CorrelationContext.inject_into_headers(headers, cid)
                headers["correlation_id"] = cid
            payload_bytes, content_type = serialize_payload(
                data, format=self.config.serialization_format
            )
            if not any(k.lower() == "content-type" for k in headers):
                headers["Content-Type"] = content_type
            payload = payload_bytes

        if self._jetstream_active:
            if not subject_covered_by(self.config.jetstream_streams, subject):
                claims = [s for spec in self.config.jetstream_streams for s in spec.subjects]
                raise StreamDeclarationError(
                    f"jetstream_enabled is on, but no declared stream covers the dead-letter "
                    f"subject {subject!r}. Declared claims: {claims}."
                )
            assert self.js is not None
            await self.js.publish(subject, payload, headers=headers)
            return

        assert self.nc is not None
        await self.nc.publish(subject, payload, headers=headers)

    async def dead_letter_decode_error(self, msg: Any, error: Exception) -> None:
        """Dead-letter a payload that failed deserialization."""
        dlq_subject = self.format_dlq_subject()
        raw = msg.data.decode(errors="replace") if getattr(msg, "data", None) else ""
        payload = {"raw": raw}
        num_delivered = getattr(getattr(msg, "metadata", None), "num_delivered", 1)

        dlq_headers: dict[str, str] = {}
        msg_h = getattr(msg, "headers", None)
        headers_map = dict(msg_h) if isinstance(msg_h, Mapping) else {}
        cid = CorrelationContext.extract_from_headers(headers_map) or CorrelationContext.get()
        if cid:
            CorrelationContext.inject_into_headers(dlq_headers, cid)
            dlq_headers["correlation_id"] = cid

        try:
            await self.publish_dlq(
                dlq_subject,
                original_subject=getattr(msg, "subject", ""),
                payload=payload,
                error=f"Decode error: {error}",
                service=self.config.name,
                deliveries=num_delivered,
                headers=dlq_headers,
            )
            self.logger.warning(
                f"Dead-lettered malformed message on '{getattr(msg, 'subject', '')}' "
                f"to '{dlq_subject}': {error}"
            )
        except Exception as dlq_error:
            self.logger.error(
                f"Failed to dead-letter malformed message on '{getattr(msg, 'subject', '')}' "
                f"to '{dlq_subject}' ({dlq_error}). Terminating anyway. Payload: {payload!r}"
            )

    async def dead_letter_terminated(self, msg: Any, error: Any, num_delivered: int) -> None:
        """Dead-letter a JetStream message that exhausted max delivery attempts."""
        from ..validation import deserialize_payload

        dlq_subject = self.format_dlq_subject()
        msg_h = getattr(msg, "headers", None)
        headers = dict(msg_h) if isinstance(msg_h, Mapping) else {}
        content_type = None
        for k, v in headers.items():
            if k.lower() == "content-type":
                content_type = v.split(";")[0].strip().lower()
                break

        try:
            payload = deserialize_payload(
                msg.data,
                content_type=content_type,
                fallback_format=self.config.serialization_format,
            )
        except Exception:
            raw = msg.data.decode(errors="replace") if getattr(msg, "data", None) else ""
            payload = {"raw": raw}

        dlq_headers: dict[str, str] = {}
        cid = (
            CorrelationContext.extract_from_headers(headers)
            or (payload.get("correlation_id") if isinstance(payload, dict) else None)
            or CorrelationContext.get()
        )
        if cid:
            CorrelationContext.inject_into_headers(dlq_headers, cid)
            dlq_headers["correlation_id"] = cid

        try:
            await self.publish_dlq(
                dlq_subject,
                original_subject=getattr(msg, "subject", ""),
                payload=payload,
                error=str(error),
                service=self.config.name,
                deliveries=num_delivered,
                headers=dlq_headers,
            )
            self.logger.warning(
                f"Dead-lettered '{getattr(msg, 'subject', '')}' to '{dlq_subject}' after "
                f"{num_delivered} deliveries: {error}"
            )
        except Exception as dlq_error:
            self.logger.error(
                f"Failed to dead-letter '{getattr(msg, 'subject', '')}' to '{dlq_subject}' "
                f"({dlq_error}). Terminating anyway. Payload: {payload!r}"
            )

    async def handle_invalid_message(
        self,
        subject: str,
        payload: Any,
        error: Any,
        schema: Any,
        on_invalid: str | None,
        correlation_id: str | None = None,
    ) -> None:
        """Route schema validation failures to DLQ or drop according to strategy."""
        strategy = on_invalid or self.config.default_on_invalid
        errors = json.loads(error.json())

        if strategy == "deadletter":
            dlq_subject = self.format_dlq_subject()
            dlq_headers: dict[str, str] = {}
            cid = (
                correlation_id
                or (payload.get("correlation_id") if isinstance(payload, dict) else None)
                or CorrelationContext.get()
            )
            if cid:
                CorrelationContext.inject_into_headers(dlq_headers, cid)
                dlq_headers["correlation_id"] = cid

            try:
                await self.publish_dlq(
                    dlq_subject,
                    original_subject=subject,
                    payload=payload,
                    errors=errors,
                    service=self.config.name,
                    schema=schema.__name__,
                    headers=dlq_headers,
                )
                self.logger.warning(
                    f"Dead-lettered invalid message on '{subject}' to '{dlq_subject}' "
                    f"({len(errors)} validation error(s))"
                )
            except Exception as dlq_error:
                self.logger.error(
                    f"Failed to dead-letter invalid message on '{subject}' to "
                    f"'{dlq_subject}' ({dlq_error}). Payload: {payload!r}"
                )
        else:
            self.logger.warning(f"Dropped invalid message on '{subject}': {errors}")

    # Compatibility aliases
    _format_dlq_subject = format_dlq_subject
    _publish_dlq = publish_dlq
    _dead_letter_decode_error = dead_letter_decode_error
    _dead_letter_terminated = dead_letter_terminated
    _handle_invalid_message = handle_invalid_message

"""Dead-letter queue evaluation, formatting, and publishing."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from loguru import logger as global_logger
from nats.errors import NotJSMessageError
from pydantic import ValidationError

from ..correlation import CorrelationContext
from ..credentials import credential_names_of, is_credential_name
from ..error_text import has_own_text, may_expose
from ..extension import RejectMessage
from ..jetstream import StreamDeclarationError, subject_covered_by
from ..service_config import ServiceConfig
from ..validation import serialize_payload

# The `cause` field of a dead-letter record: why the message was dead-lettered. `cliffracer-dlq`
# lists and counts by it, so the record says it in a field and no reader infers it from the text
# of `error`, which a handler's own exception can write.
CAUSE_DECODE = "decode"
CAUSE_DELIVERY_LIMIT = "delivery-limit"
CAUSE_INVALID = "invalid"


def message_metadata(msg: Any) -> Any:
    """The JetStream metadata of `msg`, or None for a message that did not come from JetStream.

    nats-py's `Msg.metadata` is a property that raises `NotJSMessageError` for a core message, and
    `getattr` with a default catches only `AttributeError`.
    """
    try:
        return getattr(msg, "metadata", None)
    except NotJSMessageError:
        return None


class DeadLetterPublisher:
    """Evaluates DLQ subject templates and publishes error/diagnostic messages.

    Invariants:
    - Never swallows stream declaration errors when JetStream is active.
    - Injects correlation_id into headers and payload if present or generatable.
    - Respects configured serialization format when converting diagnostic payloads.
    - Genuine transport publisher with zero circular container dependencies.
    - A dead letter is best-effort: when its publish fails, the failure is logged with the
      payload and counted in `lost`, and the caller still terminates the delivery. The three
      handlers below return `False` for exactly that, and `True` when nothing was lost.
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
        # How many dead letters a handler below could not publish since this publisher was built.
        self.lost = 0

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

    def _credential_headers(self) -> frozenset[str]:
        """Names of the headers an installed extension reads a credential from."""
        container = getattr(self.service, "container", None)
        return credential_names_of(getattr(container, "extensions", None))

    def origin(self, msg: Any) -> tuple[dict[str, Any], dict[str, str]]:
        """Where a message came from, as record fields, and the header that identifies the record.

        `original_headers` is every header the message arrived with except the ones that carry a
        credential, which `withheld_headers` names. `stream`, `stream_sequence` and `consumer`
        are the JetStream delivery's own coordinates, present only when the server reported them.
        The `Nats-Msg-Id` is built from those three and the service name, so that publishing the
        same delivery's dead letter again within the stream's duplicate window stores it once;
        a delivery with no coordinates gets none.
        """
        fields: dict[str, Any] = {}
        raw = getattr(msg, "headers", None)
        if isinstance(raw, Mapping) and raw:
            configured = self._credential_headers()
            kept = {str(k): str(v) for k, v in raw.items() if not is_credential_name(k, configured)}
            withheld = sorted(str(k) for k in raw if is_credential_name(k, configured))
            if kept:
                fields["original_headers"] = kept
            if withheld:
                fields["withheld_headers"] = withheld

        meta = message_metadata(msg)
        stream = getattr(meta, "stream", None)
        consumer = getattr(meta, "consumer", None)
        sequence = getattr(getattr(meta, "sequence", None), "stream", None)
        if isinstance(stream, str) and stream:
            fields["stream"] = stream
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            fields["stream_sequence"] = sequence
        if isinstance(consumer, str) and consumer:
            fields["consumer"] = consumer

        headers: dict[str, str] = {}
        if {"stream", "stream_sequence", "consumer"} <= fields.keys():
            headers["Nats-Msg-Id"] = (
                f"dlq:{self.config.name}:{fields['stream']}:{fields['stream_sequence']}"
                f":{fields['consumer']}"
            )
        return fields, headers

    def format_dlq_subject(self) -> str:
        """Format configured dead-letter subject template."""
        from cliffracer.core.discovery import HandlerDiscovery

        return HandlerDiscovery.dlq_subject(self.config)

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
            # The headers first: every caller puts the message's id there, and
            # the decode-error and last-delivery paths run with no ambient
            # context set, so reading only the record and the context minted a
            # new id and wrote it over the header this was handed.
            cid = (
                data.get("correlation_id")
                or CorrelationContext.extract_from_headers(headers)
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
            declared = self.config.effective_jetstream_streams
            if not subject_covered_by(declared, subject):
                claims = [s for spec in declared for s in spec.subjects]
                raise StreamDeclarationError(
                    f"jetstream_enabled is on, but no declared stream covers the dead-letter "
                    f"subject {subject!r}. Declared claims: {claims}."
                )
            js = self.js
            if js is None:
                raise ConnectionError(
                    f"cannot publish the dead letter on {subject!r}: JetStream is active but "
                    f"service {self.config.name!r} has no JetStream context (not connected)"
                )
            await js.publish(subject, payload, headers=headers)
            return

        nc = self.nc
        if nc is None:
            raise ConnectionError(
                f"cannot publish the dead letter on {subject!r}: "
                f"service {self.config.name!r} has no NATS connection"
            )
        await nc.publish(subject, payload, headers=headers)

    async def dead_letter_decode_error(self, msg: Any, error: Exception) -> bool:
        """Dead-letter a payload that failed deserialization.

        Returns `False` when the dead letter could not be published, which is counted in `lost`.
        """
        dlq_subject = self.format_dlq_subject()
        raw = msg.data.decode(errors="replace") if getattr(msg, "data", None) else ""
        payload = {"raw": raw}
        num_delivered = getattr(message_metadata(msg), "num_delivered", 1)

        try:
            dlq_headers: dict[str, str] = {}
            msg_h = getattr(msg, "headers", None)
            headers_map = dict(msg_h) if isinstance(msg_h, Mapping) else {}
            # Never the ambient context: this runs before any dispatch has set one, in a
            # task that copied whatever the starting context held, so reading it stamps
            # every undecodable message with the same unrelated id.
            cid = CorrelationContext.new_id_unless_given(
                CorrelationContext.extract_from_headers(headers_map)
            )
            CorrelationContext.inject_into_headers(dlq_headers, cid)
            dlq_headers["correlation_id"] = cid
            origin_fields, origin_headers = self.origin(msg)
            dlq_headers.update(origin_headers)

            await self.publish_dlq(
                dlq_subject,
                original_subject=getattr(msg, "subject", ""),
                payload=payload,
                error=f"Decode error: {error}",
                cause=CAUSE_DECODE,
                service=self.config.name,
                deliveries=num_delivered,
                headers=dlq_headers,
                **origin_fields,
            )
            self.logger.warning(
                f"Dead-lettered malformed message on '{getattr(msg, 'subject', '')}' "
                f"to '{dlq_subject}': {error}"
            )
            return True
        except Exception as dlq_error:
            self.lost += 1
            self.logger.error(
                f"Failed to dead-letter malformed message on '{getattr(msg, 'subject', '')}' "
                f"to '{dlq_subject}' ({type(dlq_error).__name__}: {dlq_error}). Terminating anyway. "
                f"Payload: {payload!r}"
            )
            return False

    def _error_that_may_leave(self, error: Any) -> str:
        """The `error` a dead letter may carry: an exception's text only when the flag lets it leave.

        The record is readable by whoever can read the dead-letter stream, so it is outside the
        process. A handler's exception is the type's name unless `expose_internal_errors` is set. The
        text the framework wrote itself is not withheld: an exception marked `own_text` (the overrun of
        `max_processing_time`, the missing msgpack package), the reason of a crashed gate, which is
        already phrased for the outside, and a message that is not an exception.
        """
        if may_expose(self.config) or not isinstance(error, BaseException):
            return str(error)
        if has_own_text(error) or (isinstance(error, RejectMessage) and error.hook_crash):
            return str(error)
        return type(error).__name__

    async def dead_letter_terminated(
        self,
        msg: Any,
        error: Any,
        num_delivered: int,
        delivery_limit: str | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        """Dead-letter a JetStream message that exhausted max delivery attempts.

        Returns `False` when the dead letter could not be published, which is counted in `lost`.

        `delivery_limit` names the limit that decided, such as
        `"server max_deliver 3"`. It goes into the record and the log line
        because the server's limit and the config's can differ, and an operator
        who reads the wrong one looks in the wrong place.
        """
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

        limit_fields = {} if delivery_limit is None else {"delivery_limit": delivery_limit}
        limit_clause = "" if delivery_limit is None else f" ({delivery_limit})"
        try:
            dlq_headers: dict[str, str] = {}
            # The id the failing delivery ran under, else the one on the wire, else a
            # new one. Never the ambient context, which by now is back to whatever the
            # task started with and belongs to no message.
            cid = CorrelationContext.new_id_unless_given(
                correlation_id
                or CorrelationContext.extract_from_headers(headers)
                or (payload.get("correlation_id") if isinstance(payload, dict) else None)
            )
            CorrelationContext.inject_into_headers(dlq_headers, cid)
            dlq_headers["correlation_id"] = cid
            origin_fields, origin_headers = self.origin(msg)
            dlq_headers.update(origin_headers)

            await self.publish_dlq(
                dlq_subject,
                original_subject=getattr(msg, "subject", ""),
                payload=payload,
                error=self._error_that_may_leave(error),
                cause=CAUSE_DELIVERY_LIMIT,
                service=self.config.name,
                deliveries=num_delivered,
                headers=dlq_headers,
                **origin_fields,
                **limit_fields,
            )
            self.logger.warning(
                f"Dead-lettered '{getattr(msg, 'subject', '')}' to '{dlq_subject}' after "
                f"{num_delivered} deliveries{limit_clause}: {error}"
            )
            return True
        except Exception as dlq_error:
            self.lost += 1
            self.logger.error(
                f"Failed to dead-letter '{getattr(msg, 'subject', '')}' to '{dlq_subject}' "
                f"({type(dlq_error).__name__}: {dlq_error}). Terminating anyway. Payload: {payload!r}"
            )
            return False

    async def handle_invalid_message(
        self,
        subject: str,
        payload: Any,
        error: Any,
        schema: Any,
        on_invalid: str | None,
        correlation_id: str | None = None,
        msg: Any = None,
    ) -> bool:
        """Route schema validation failures to DLQ or drop according to strategy.

        Returns `False` only when a dead letter was wanted and could not be published, which is
        counted in `lost`. A message the strategy drops on purpose loses nothing, and returns `True`.
        `msg`, the delivery being refused, supplies the record's origin fields when it is given.
        """
        strategy = on_invalid or self.config.default_on_invalid
        if isinstance(error, ValidationError):
            errors = json.loads(error.json())
        else:
            # Validation raised something pydantic does not wrap, such as a TypeError from a
            # model validator: there is no list of rule failures, so the record carries the one.
            errors = [
                {"type": "validator_raised", "loc": [], "msg": f"{type(error).__name__}: {error}"}
            ]

        if strategy == "deadletter":
            dlq_subject = self.format_dlq_subject()
            try:
                dlq_headers: dict[str, str] = {}
                # The first candidate that is a string: a payload field is any JSON value, and a
                # number or an object in `correlation_id` is not an id.
                cid = next(
                    (
                        candidate
                        for candidate in (
                            correlation_id,
                            payload.get("correlation_id") if isinstance(payload, dict) else None,
                            CorrelationContext.get(),
                        )
                        if isinstance(candidate, str) and candidate
                    ),
                    None,
                )
                if cid:
                    CorrelationContext.inject_into_headers(dlq_headers, cid)
                    dlq_headers["correlation_id"] = cid
                origin_fields, origin_headers = self.origin(msg)
                dlq_headers.update(origin_headers)

                await self.publish_dlq(
                    dlq_subject,
                    original_subject=subject,
                    payload=payload,
                    errors=errors,
                    cause=CAUSE_INVALID,
                    service=self.config.name,
                    schema=schema.__name__,
                    headers=dlq_headers,
                    **origin_fields,
                )
                self.logger.warning(
                    f"Dead-lettered invalid message on '{subject}' to '{dlq_subject}' "
                    f"({len(errors)} validation error(s))"
                )
            except Exception as dlq_error:
                self.lost += 1
                self.logger.error(
                    f"Failed to dead-letter invalid message on '{subject}' to "
                    f"'{dlq_subject}' ({type(dlq_error).__name__}: {dlq_error}). Payload: {payload!r}"
                )
                return False
        else:
            self.logger.warning(f"Dropped invalid message on '{subject}': {errors}")
        return True

    # Compatibility aliases
    _format_dlq_subject = format_dlq_subject
    _publish_dlq = publish_dlq
    _dead_letter_decode_error = dead_letter_decode_error
    _dead_letter_terminated = dead_letter_terminated
    _handle_invalid_message = handle_invalid_message

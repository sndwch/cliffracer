"""Resilient acknowledgement and shutdown drain middleware for FastStream."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from faststream.middlewares import BaseMiddleware
from loguru import logger

if TYPE_CHECKING:
    from faststream._internal.context.repository import ContextRepo


def _extract_raw_message(msg: Any) -> Any:
    """Extract underlying NATS message from FastStream StreamMessage or return message."""
    if hasattr(msg, "raw_message") and type(msg).__name__ in ("StreamMessage", "NatsMessage"):
        return msg.raw_message
    return msg


def _is_jetstream_message(raw_msg: Any) -> bool:
    """Safely determine whether a message is JetStream without raising NotJSMessageError."""
    try:
        metadata = getattr(raw_msg, "metadata", None)
        return metadata is not None
    except Exception:
        return False


class _SafeHeartbeat:
    """In-progress heartbeat context manager for active JetStream messages."""

    def __init__(self, container: Any, msg: Any, interval: float) -> None:
        self.container = container
        self.msg = msg
        self.interval = interval
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _SafeHeartbeat:
        if (
            self.interval > 0
            and hasattr(self.msg, "in_progress")
            and callable(self.msg.in_progress)
        ):
            self._task = asyncio.create_task(self._pulse_loop(), name="faststream_heartbeat")
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _pulse_loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                if self.container is not None and hasattr(self.container, "_safe_in_progress"):
                    res = self.container._safe_in_progress(self.msg)
                    if inspect.isawaitable(res):
                        await res
                elif hasattr(self.msg, "in_progress") and callable(self.msg.in_progress):
                    res = self.msg.in_progress()
                    if inspect.isawaitable(res):
                        await res
            except Exception as exc:
                logger.debug("Failed in-progress heartbeat pulse: {}", exc)


class CliffracerAckMiddleware(BaseMiddleware):
    """Resilient acknowledgement and task drain middleware for FastStream.

    Invariants:
    - Enforces AckPolicy.MANUAL semantics to prevent silent message loss under REJECT_ON_ERROR.
    - Registers asyncio.current_task() in container._active_tasks for Step 4 shutdown drain.
    - Automatically executes _safe_ack() on successful handler execution.
    - Negative-acknowledges with exponential backoff on transient operational failures.
    - Routes exhausted deliveries (num_delivered >= max_deliver) and validation errors to DLQ,
      then terminates the message.
    - Immediately acknowledges policy refusals (RejectMessage).
    - Pulses in-progress status via heartbeat for long-running executions.
    """

    def __init__(
        self,
        msg: Any = None,
        /,
        *,
        context: ContextRepo | None = None,
        container: Any = None,
        config: Any = None,
    ) -> None:
        super().__init__(msg, context=context)  # type: ignore[arg-type]
        self.container = container
        if self.container is None and context is not None:
            try:
                self.container = context.get("container")
            except Exception:
                pass

        self.config = config
        if self.config is None and context is not None:
            try:
                self.config = context.get("config")
            except Exception:
                pass
        if self.config is None and self.container is not None:
            self.config = getattr(self.container, "config", None)

    async def consume_scope(
        self,
        call_next: Callable[[Any], Awaitable[Any]],
        msg: Any,
    ) -> Any:
        """Wrap message execution with task drain tracking and resilient ack lifecycle."""
        current_task = asyncio.current_task()
        active_tasks = (
            getattr(self.container, "_active_tasks", None) if self.container is not None else None
        )
        if current_task is not None and active_tasks is not None:
            active_tasks.add(current_task)

        try:
            raw_msg = _extract_raw_message(msg)
            # Check if this is a JetStream message
            is_jetstream = _is_jetstream_message(raw_msg)

            if not is_jetstream:
                # Core NATS: no ack/nak/term capabilities
                return await call_next(msg)

            # JetStream message handling
            ack_wait = 30.0
            if self.config is not None:
                val = getattr(self.config, "jetstream_ack_wait", 30.0)
                if isinstance(val, int | float):
                    ack_wait = float(val)

            pulse_interval = max(0.05, ack_wait / 2.0) if ack_wait > 0 else 0.0
            heartbeat = _SafeHeartbeat(self.container, raw_msg, pulse_interval)

            try:
                async with heartbeat:
                    result = await call_next(msg)
            except Exception as exc:
                if self._is_reject_message(exc):
                    await self._safe_ack(raw_msg)
                    return None

                if self._is_validation_or_decode_error(exc):
                    num_delivered = self._get_num_delivered(raw_msg)
                    await self._publish_dlq_and_term(raw_msg, exc, num_delivered, is_decode=True)
                    return None

                num_delivered = self._get_num_delivered(raw_msg)
                max_deliver = 5
                if self.config is not None:
                    max_val = getattr(self.config, "jetstream_max_deliver", 5)
                    if isinstance(max_val, int):
                        max_deliver = max_val

                if num_delivered >= max_deliver:
                    await self._publish_dlq_and_term(raw_msg, exc, num_delivered, is_decode=False)
                else:
                    delay = self._calculate_nak_delay(num_delivered)
                    logger.warning(
                        "Transient handler failure on '{}' (delivery {}/{}): {}. NAKing with delay={}s",
                        getattr(raw_msg, "subject", ""),
                        num_delivered,
                        max_deliver,
                        exc,
                        delay,
                    )
                    await self._safe_nak(raw_msg, delay=delay)
                return None

            await self._safe_ack(raw_msg)
            return result
        finally:
            if current_task is not None and active_tasks is not None:
                active_tasks.discard(current_task)

    def _get_num_delivered(self, raw_msg: Any) -> int:
        try:
            metadata = getattr(raw_msg, "metadata", None)
            if metadata is not None:
                num = getattr(metadata, "num_delivered", 1)
                if isinstance(num, int):
                    return num
                if hasattr(num, "__int__"):
                    return int(num)
        except Exception:
            pass
        return 1

    def _calculate_nak_delay(self, num_delivered: int) -> float:
        base = 1.0
        cap = 60.0
        if self.config is not None:
            b_val = getattr(self.config, "jetstream_nak_backoff", 1.0)
            if isinstance(b_val, int | float):
                base = float(b_val)
            c_val = getattr(self.config, "jetstream_max_backoff", 60.0)
            if isinstance(c_val, int | float):
                cap = float(c_val)

        exponent = max(num_delivered - 1, 0)
        return float(min(base * (2**exponent), cap))

    def _is_reject_message(self, exc: BaseException) -> bool:
        if exc.__class__.__name__ == "RejectMessage":
            return True
        return False

    def _is_validation_or_decode_error(self, exc: BaseException) -> bool:
        if isinstance(exc, json.JSONDecodeError | UnicodeDecodeError):
            return True
        cls_name = exc.__class__.__name__
        if (
            "ValidationError" in cls_name
            or "DecodeError" in cls_name
            or "JSONDecodeError" in cls_name
        ):
            return True
        return False

    async def _safe_ack(self, raw_msg: Any) -> bool:
        if getattr(raw_msg, "_ackd", False):
            return True
        if self.container is not None and hasattr(self.container, "_safe_ack"):
            res = self.container._safe_ack(raw_msg)
            ok = await res if inspect.isawaitable(res) else res
            if ok:
                try:
                    raw_msg._ackd = True
                except Exception:
                    pass
            return bool(ok)
        if hasattr(raw_msg, "ack") and callable(raw_msg.ack):
            try:
                res = raw_msg.ack()
                if inspect.isawaitable(res):
                    await res
                try:
                    raw_msg._ackd = True
                except Exception:
                    pass
                return True
            except Exception as exc:
                logger.warning("Failed to ACK JetStream message: {}", exc)
                return False
        return False

    async def _safe_nak(self, raw_msg: Any, delay: float = 0.0) -> bool:
        if self.container is not None and hasattr(self.container, "_safe_nak"):
            res = self.container._safe_nak(raw_msg, delay=delay)
            ok = await res if inspect.isawaitable(res) else res
            return bool(ok)
        if hasattr(raw_msg, "nak") and callable(raw_msg.nak):
            try:
                res = raw_msg.nak(delay=delay) if delay > 0 else raw_msg.nak()
                if inspect.isawaitable(res):
                    await res
                return True
            except Exception as exc:
                logger.warning("Failed to NAK JetStream message: {}", exc)
                return False
        return False

    async def _safe_term(self, raw_msg: Any) -> bool:
        if self.container is not None and hasattr(self.container, "_safe_term"):
            res = self.container._safe_term(raw_msg)
            ok = await res if inspect.isawaitable(res) else res
            return bool(ok)
        if hasattr(raw_msg, "term") and callable(raw_msg.term):
            try:
                res = raw_msg.term()
                if inspect.isawaitable(res):
                    await res
                return True
            except Exception as exc:
                logger.warning("Failed to TERM JetStream message: {}", exc)
                return False
        return False

    async def _publish_dlq_and_term(
        self,
        raw_msg: Any,
        error: Exception,
        num_delivered: int,
        is_decode: bool = False,
    ) -> None:
        try:
            await self._publish_dlq_diagnostic(
                raw_msg, error, num_delivered, is_decode_error=is_decode
            )
        except Exception as dlq_err:
            logger.error("DLQ publication failed ({}). Terminating anyway.", dlq_err)
        await self._safe_term(raw_msg)

    async def _publish_dlq_diagnostic(
        self,
        raw_msg: Any,
        error: Exception,
        num_delivered: int,
        is_decode_error: bool = False,
    ) -> None:
        service_name = "service"
        if self.config is not None:
            name_val = getattr(self.config, "name", "service")
            if isinstance(name_val, str):
                service_name = name_val

        dlq_subject = f"{service_name}.dlq"
        if self.config and hasattr(self.config, "dlq_subject"):
            dlq_template = getattr(self.config, "dlq_subject", None)
            if isinstance(dlq_template, str):
                namespace_val = getattr(self.config, "namespace", "")
                namespace_str = namespace_val if isinstance(namespace_val, str) else ""
                dlq_subject = dlq_template.format(
                    service=service_name,
                    namespace=namespace_str,
                )

        # 1. Prefer container's dedicated methods if available
        if self.container is not None:
            if is_decode_error and hasattr(self.container, "_dead_letter_decode_error"):
                res = self.container._dead_letter_decode_error(raw_msg, error)
                if inspect.isawaitable(res):
                    await res
                return
            if not is_decode_error and hasattr(self.container, "_dead_letter_terminated"):
                res = self.container._dead_letter_terminated(raw_msg, error, num_delivered)
                if inspect.isawaitable(res):
                    await res
                return
            if hasattr(self.container, "_publish_dlq"):
                raw_data = getattr(raw_msg, "data", b"")
                raw_str = (
                    raw_data.decode(errors="replace")
                    if isinstance(raw_data, bytes)
                    else str(raw_data)
                )
                payload = {"raw": raw_str}
                res = self.container._publish_dlq(
                    dlq_subject,
                    original_subject=getattr(raw_msg, "subject", ""),
                    payload=payload,
                    error=str(error),
                    service=service_name,
                    deliveries=num_delivered,
                )
                if inspect.isawaitable(res):
                    await res
                return

        # 2. Fallback to container.nc if present
        nc = getattr(self.container, "nc", None) if self.container is not None else None
        if nc is not None and hasattr(nc, "publish"):
            raw_data = getattr(raw_msg, "data", b"")
            raw_str = (
                raw_data.decode(errors="replace") if isinstance(raw_data, bytes) else str(raw_data)
            )
            diag = json.dumps(
                {
                    "service": service_name,
                    "original_subject": getattr(raw_msg, "subject", ""),
                    "error": str(error),
                    "deliveries": num_delivered,
                    "raw": raw_str,
                }
            ).encode("utf-8")
            res = nc.publish(dlq_subject, diag)
            if inspect.isawaitable(res):
                await res

"""Domain event routing, schema validation, and dispatch pipeline."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from enum import Enum
from typing import Any

from loguru import logger as global_logger
from pydantic import ValidationError as _PydValidationError

from ..extension import RejectMessage, WorkerContext
from ..registry import ServiceRegistry
from ..service_config import ServiceConfig
from ..subjects import subject_matches
from ..validation import deserialize_payload
from .dlq import DeadLetterPublisher
from .pipeline import ExtensionPipeline


class DispatchOutcome(Enum):
    """The terminal outcome of an event dispatch execution."""

    OK = "ok"
    INVALID = "invalid"


class _HandlerMeta:
    __slots__ = (
        "is_async",
        "has_subject",
        "has_correlation_id",
        "has_var_kw",
        "has_explicit_data_param",
        "param_names",
    )

    def __init__(self, handler: Callable[..., Any]):
        self.is_async = inspect.iscoroutinefunction(handler)
        sig = inspect.signature(handler)
        self.param_names = set(sig.parameters.keys())
        self.has_subject = "subject" in self.param_names
        self.has_correlation_id = "correlation_id" in self.param_names
        self.has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        self.has_explicit_data_param = any(
            name == "data"
            and p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            for name, p in sig.parameters.items()
        )


class EventDispatcher:
    """Inbound domain event dispatching, pattern matching, and schema validation.

    Invariants:
    - Matches subject against registered wildcard subscription patterns.
    - Decodes payload with fallback to service default serialization format.
    - Validates against registered Pydantic event schemas before invoking handler.
    - Emits invalid event to DLQ on schema validation error.
    - Executes handler inside ExtensionPipeline.run_worker.
    """

    def __init__(
        self,
        registry: ServiceRegistry,
        config: ServiceConfig,
        pipeline: ExtensionPipeline,
        dlq: DeadLetterPublisher,
        task_spawner: Callable[[Coroutine[Any, Any, Any], str | None], asyncio.Task[Any]]
        | None = None,
        logger: Any = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.pipeline = pipeline
        self.dlq = dlq
        self.task_spawner = task_spawner
        self.logger = logger or global_logger.bind(service=config.name)

        self._event_semaphore: asyncio.Semaphore | None = None

    def _spawn_task(
        self, coro: Coroutine[Any, Any, Any], name: str | None = None
    ) -> asyncio.Task[Any]:
        if self.task_spawner is not None:
            return self.task_spawner(coro, name)
        return asyncio.create_task(coro, name=name)

    def get_event_semaphore(self) -> asyncio.Semaphore | None:
        """Get or initialize the event concurrency semaphore."""
        limit = self.config.max_event_concurrency
        if self._event_semaphore is None and limit is not None and limit > 0:
            self._event_semaphore = asyncio.Semaphore(limit)
        return self._event_semaphore

    def _get_handler_meta(self, handler: Callable[..., Any]) -> _HandlerMeta:
        meta = getattr(handler, "_cliffracer_sig_meta", None)
        if meta is None:
            meta = _HandlerMeta(handler)
            try:
                handler._cliffracer_sig_meta = meta  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass
        return meta

    def make_event_callback(self, pattern: str) -> Callable[[Any], Awaitable[None]]:
        """Construct a NATS message callback dispatching core events for a pattern."""

        async def _cb(msg: Any) -> None:
            sem = self.get_event_semaphore()
            if sem is not None:
                await sem.acquire()
                self._spawn_task(
                    self._bounded_handle_event(msg, pattern, sem),
                    name=f"event_bounded:{pattern}",
                )
            else:
                self._spawn_task(
                    self.handle_event(msg, pattern=pattern, raise_on_error=False),
                    name=f"event:{pattern}",
                )

        return _cb

    async def _bounded_handle_event(self, msg: Any, pattern: str, sem: asyncio.Semaphore) -> None:
        try:
            await self.handle_event(msg, pattern=pattern, raise_on_error=False)
        finally:
            sem.release()

    async def safe_term(self, msg: Any) -> bool:
        """Safely terminate a JetStream message to prevent redelivery."""
        try:
            if hasattr(msg, "term") and callable(msg.term):
                await msg.term()
                return True
        except Exception as exc:
            self.logger.warning(f"Failed to TERM message on '{getattr(msg, 'subject', '')}': {exc}")
        return False

    async def _run_worker(self, ctx: WorkerContext, call: Callable[[], Awaitable[Any]]) -> Any:
        return await self.pipeline.run_worker(ctx, call)

    async def handle_event(
        self, msg: Any, *, pattern: str | None = None, raise_on_error: bool = False
    ) -> DispatchOutcome:
        """Dispatch an event message to matching handlers."""
        subject = msg.subject
        outcome = DispatchOutcome.OK

        if pattern is not None:
            handler = self.registry.event_handlers.get(pattern)
            matching_handlers = [handler] if handler is not None else []
        else:
            matching_handlers = []
            for p, handler in self.registry.event_handlers.items():
                if subject_matches(p, subject):
                    matching_handlers.append(handler)

        if not matching_handlers:
            return DispatchOutcome.OK

        msg_h = getattr(msg, "headers", None)
        headers = dict(msg_h) if isinstance(msg_h, Mapping) else {}
        content_type = None
        for k, v in headers.items():
            if k.lower() == "content-type":
                content_type = v.split(";")[0].strip().lower()
                break

        try:
            data = deserialize_payload(
                msg.data,
                content_type=content_type,
                fallback_format=self.config.serialization_format,
            )
        except Exception as e:
            self.logger.error(f"Error decoding payload for event on {subject}: {e}")
            await self.dlq.dead_letter_decode_error(msg, e)
            return DispatchOutcome.INVALID

        is_enveloped = (
            isinstance(data, dict)
            and "data" in data
            and "source_service" in data
            and "timestamp" in data
        )
        domain_payload = data["data"] if is_enveloped else data

        for handler in matching_handlers:
            ctx = WorkerContext(
                kind="event",
                subject=subject,
                headers=headers,
                correlation_id=None,
                payload=data,
                raw=msg,
            )
            if is_enveloped:
                ctx.data["envelope"] = data

            async def call(
                handler: Any = handler,
                data: Any = data,
                domain_payload: Any = domain_payload,
                ctx: Any = ctx,
            ) -> Any:
                nonlocal outcome
                if (
                    not ctx.correlation_id
                    and is_enveloped
                    and isinstance(data, dict)
                    and data.get("correlation_id")
                ):
                    ctx.correlation_id = str(data["correlation_id"])
                self.logger.info(f"Event {subject} with correlation_id: {ctx.correlation_id}")

                meta = self._get_handler_meta(handler)

                # Typed event listeners: validate against EventHandlerSpec before dispatch
                spec = self.registry.event_specs_by_subject.get(subject)
                if spec is None and pattern is not None:
                    spec = self.registry.event_specs_by_subject.get(pattern)
                if spec is None:
                    handler_name = self.registry.event_handler_names.get(subject) or getattr(
                        handler, "__name__", None
                    )
                    if handler_name:
                        spec = self.registry.event_specs.get(handler_name)

                if spec is not None:
                    call_kwargs: dict[str, Any]
                    try:
                        if spec.is_single_model_param:
                            if isinstance(domain_payload, dict):
                                clean_payload = {
                                    k: v
                                    for k, v in domain_payload.items()
                                    if not (
                                        k == "correlation_id"
                                        and "correlation_id" not in spec.payload_model.model_fields
                                    )
                                }
                                model = spec.payload_model(**clean_payload)
                            else:
                                model = spec.payload_model.model_validate(domain_payload)
                            call_kwargs = {spec.single_model_param_name: model}  # type: ignore[dict-item]
                        else:
                            if isinstance(domain_payload, dict):
                                clean_payload = {
                                    k: v
                                    for k, v in domain_payload.items()
                                    if not (
                                        k == "correlation_id"
                                        and "correlation_id" not in spec.payload_model.model_fields
                                    )
                                }
                                validated = spec.payload_model(**clean_payload)
                            elif domain_payload is None and not spec.payload_model.model_fields:
                                validated = spec.payload_model()
                            elif (
                                not isinstance(domain_payload, dict)
                                and len(spec.payload_model.model_fields) == 1
                            ):
                                field_name = next(iter(spec.payload_model.model_fields))
                                validated = spec.payload_model.model_validate(
                                    {field_name: domain_payload}
                                )
                            else:
                                validated = spec.payload_model.model_validate(domain_payload)
                            call_kwargs = validated.model_dump()
                    except _PydValidationError as ve:
                        await self.dlq.handle_invalid_message(
                            subject,
                            data,
                            ve,
                            spec.payload_model,
                            on_invalid=None,
                            correlation_id=ctx.correlation_id,
                        )
                        outcome = DispatchOutcome.INVALID
                        await self.safe_term(msg)
                        return None

                    if spec.takes_subject:
                        call_kwargs["subject"] = subject
                    if spec.takes_correlation_id:
                        call_kwargs["correlation_id"] = ctx.correlation_id

                    if inspect.iscoroutinefunction(handler):
                        return await handler(**call_kwargs)
                    return handler(**call_kwargs)

                # Validated listeners: validate before dispatch
                if handler in self.registry.event_schemas:
                    schema, on_invalid = self.registry.event_schemas[handler]
                    try:
                        if isinstance(domain_payload, dict):
                            payload = {
                                k: v for k, v in domain_payload.items() if k != "correlation_id"
                            }
                            model = schema(**payload)
                        else:
                            model = schema.model_validate(domain_payload)
                    except _PydValidationError as ve:
                        await self.dlq.handle_invalid_message(
                            subject, data, ve, schema, on_invalid, correlation_id=ctx.correlation_id
                        )
                        outcome = DispatchOutcome.INVALID
                        return None

                    call_kwargs = {"message": model}
                    if meta.has_subject:
                        call_kwargs["subject"] = subject
                    if meta.has_correlation_id:
                        call_kwargs["correlation_id"] = ctx.correlation_id
                    if meta.is_async:
                        return await handler(**call_kwargs)
                    return handler(**call_kwargs)

                if (
                    is_enveloped
                    and meta.has_explicit_data_param
                    and not meta.has_var_kw
                    and not any(
                        k in meta.param_names
                        for k in (domain_payload.keys() if isinstance(domain_payload, dict) else ())
                    )
                ):
                    kwargs = {"data": domain_payload}
                else:
                    kwargs = (
                        dict(domain_payload)
                        if isinstance(domain_payload, dict)
                        else {"data": domain_payload}
                    )
                    if is_enveloped and meta.has_explicit_data_param and "data" not in kwargs:
                        kwargs["data"] = domain_payload

                if meta.has_correlation_id:
                    kwargs["correlation_id"] = ctx.correlation_id
                else:
                    kwargs.pop("correlation_id", None)

                if meta.has_subject:
                    if meta.is_async:
                        return await handler(subject=subject, **kwargs)
                    return handler(subject=subject, **kwargs)

                if meta.is_async:
                    return await handler(**kwargs)
                return handler(**kwargs)

            try:
                await self._run_worker(ctx, call)
            except RejectMessage as e:
                self.logger.warning(
                    f"event {subject} refused by an extension "
                    f"(correlation_id: {ctx.correlation_id}): {e.reason}"
                )
            except Exception as e:
                if raise_on_error:
                    raise
                self.logger.error(
                    f"Error handling event {subject} (correlation_id: {ctx.correlation_id}): {e}"
                )

        return outcome

    # Compatibility aliases
    _get_event_semaphore = get_event_semaphore
    _handle_event = handle_event

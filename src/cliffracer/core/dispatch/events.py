"""Domain event routing, schema validation, and dispatch pipeline."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from enum import Enum
from typing import Any

from loguru import logger as global_logger
from pydantic import AliasChoices, AliasPath, BaseModel, RootModel
from pydantic import ValidationError as _PydValidationError

from ..correlation import CorrelationContext
from ..extension import RejectMessage, RetryMessage, WorkerContext
from ..messages import with_correlation_id
from ..registry import ServiceRegistry
from ..service_config import ServiceConfig
from ..subjects import subject_matches
from ..validation import deserialize_payload, validate_decoded, validate_payload
from .dlq import DeadLetterPublisher
from .handler_limits import HandlerLimit, HandlerLimits, release, take
from .pipeline import ExtensionPipeline

_CORRELATION_ID_ATTR = "_cliffracer_correlation_id"


def carry_correlation_id(error: BaseException, correlation_id: str | None) -> None:
    """Record on `error` the id its dispatch ran under, for the layer that handles it next.

    `handle_event` re-raises to the transport layer, which decides the message's fate
    and may dead-letter it after the dispatch's context has been reset, so the id is
    handed over on the exception rather than read from the context.
    """
    if correlation_id:
        try:
            setattr(error, _CORRELATION_ID_ATTR, correlation_id)
        except (AttributeError, TypeError):
            pass  # an exception type that takes no attributes: the transport mints one


def carried_correlation_id(error: BaseException) -> str | None:
    """The id `carry_correlation_id` recorded on `error`, if any."""
    value = getattr(error, _CORRELATION_ID_ATTR, None)
    return value if isinstance(value, str) else None


def _keys_a_model_reads(model: type[BaseModel]) -> set[str]:
    """Every key a model declares for one of its fields: a field's name, its alias, and its
    validation alias.

    A validation alias is a name, an `AliasChoices` of names and paths (each choice counts, and
    a path by its first element), or an `AliasPath`. A key counts whether or not the model's config
    reads it: an alias counts under `validate_by_alias=False` too, so a payload holding only that
    key is read flat, as it was.
    """
    keys: set[str] = set()

    def add(alias: str | AliasPath | AliasChoices | None) -> None:
        if isinstance(alias, str):
            keys.add(alias)
        elif isinstance(alias, AliasChoices):
            for choice in alias.choices:
                add(choice)
        elif isinstance(alias, AliasPath) and alias.path and isinstance(alias.path[0], str):
            keys.add(alias.path[0])

    for name, info in model.model_fields.items():
        keys.add(name)
        add(info.alias)
        add(info.validation_alias)
    return keys


def _the_model_in(payload: Any, parameter: str | None, model: type[BaseModel]) -> Any:
    """The object a single-model listener reads as its model.

    `publish_event(topic, item=Model(...))` sends `{"item": {...}}`, and a listener `on(self, item:
    Model)` is as likely to be sent the model's own fields. Both are read: the payload is the
    parameter's object when no field of the model is named or aliased like the parameter and the
    payload is exactly `{parameter: object}`, apart from the `correlation_id` an event carries and
    a model does not read. Any other payload is read as it is, so a model with a field of that
    name or alias, a `RootModel`, a payload with another key, and a value that is not an object
    are read flat.
    """
    if parameter is None or issubclass(model, RootModel) or not isinstance(payload, Mapping):
        return payload
    read = _keys_a_model_reads(model)
    if parameter in read:
        return payload
    keys = {key for key in payload if not (key == "correlation_id" and key not in read)}
    if keys == {parameter} and isinstance(payload[parameter], Mapping):
        return payload[parameter]
    return payload


class DispatchOutcome(Enum):
    """The terminal outcome of an event dispatch execution.

    ``NO_HANDLER`` separates "nothing was listening" from "a handler ran", which
    are otherwise indistinguishable to a caller. It is not ``INVALID``, so a
    JetStream consumer acknowledges it as it does ``OK``: no handler is
    registered for the subject, and redelivering the message will not change
    that.
    """

    OK = "ok"
    INVALID = "invalid"
    NO_HANDLER = "no_handler"


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
        limits: HandlerLimits | None = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.pipeline = pipeline
        self.dlq = dlq
        self.limits = limits if limits is not None else HandlerLimits()
        self.task_spawner = task_spawner
        self.logger = logger or global_logger.bind(service=config.name)

        self._event_semaphore: asyncio.Semaphore | None = None
        self._handler_metas: dict[Any, _HandlerMeta] = {}

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
        """The parsed signature of `handler`, read once.

        Kept in a table of the dispatcher's own and not as an attribute of the handler: a listener
        declared on a service class is registered as a bound method, which takes no attributes.
        """
        try:
            meta = self._handler_metas.get(handler)
        except TypeError:  # a callable that cannot be hashed is read each time
            return _HandlerMeta(handler)
        if meta is None:
            meta = self._handler_metas[handler] = _HandlerMeta(handler)
        return meta

    def make_event_callback(self, pattern: str) -> Callable[[Any], Awaitable[None]]:
        """Construct a NATS message callback dispatching core events for a pattern."""

        async def _cb(msg: Any) -> None:
            sem, held = self.get_event_semaphore(), self.limit_of(pattern)
            if sem is not None or held is not None:
                # The handler's permit first, then the service's. This subscription carries only
                # this pattern's messages, which are this handler's, so waiting here holds back
                # no other handler. With no deadline on an event, `take` waits until it has both.
                await take(held, sem, None)
                # The permits are returned by the task's done-callback, not by the
                # coroutine. A task cancelled before its first execution never runs
                # its body, so a release inside it would not happen and the permits
                # would be lost for the life of the process. A done-callback runs
                # exactly once for every task: completed, raised, or cancelled
                # before starting.
                task = self._spawn_task(
                    self._bounded_handle_event(msg, pattern),
                    name=f"event_bounded:{pattern}",
                )
                task.add_done_callback(lambda _finished: release(held, sem))
            else:
                self._spawn_task(
                    self.handle_event(msg, pattern=pattern, raise_on_error=False),
                    name=f"event:{pattern}",
                )

        return _cb

    def limit_of(self, pattern: str | None) -> HandlerLimit | None:
        """The concurrency limit of the handler listening on `pattern`, or None."""
        return (
            None if pattern is None else self.limits.of(self.registry.event_handlers.get(pattern))
        )

    async def _bounded_handle_event(self, msg: Any, pattern: str) -> None:
        await self.handle_event(msg, pattern=pattern, raise_on_error=False)

    def _report_unexpected_validation_failure(
        self, error: Exception, subject: str, ctx: WorkerContext
    ) -> None:
        """Log a failure of the payload's validation that pydantic did not report as invalid.

        A validator that raises something other than `ValueError` or `AssertionError` is a defect
        in the validator, not in the message; the message is refused all the same, and this is
        where the defect is named.
        """
        if isinstance(error, _PydValidationError):
            return
        self.logger.error(
            f"Validating event {subject} raised {type(error).__name__}: {error} "
            f"(correlation_id: {ctx.correlation_id}); the message is refused as invalid"
        )

    async def _run_worker(self, ctx: WorkerContext, call: Callable[[], Awaitable[Any]]) -> Any:
        return await self.pipeline.run_worker(ctx, call)

    async def handle_event(
        self,
        msg: Any,
        *,
        pattern: str | None = None,
        raise_on_error: bool = False,
        ran_under: list[str] | None = None,
    ) -> DispatchOutcome:
        """Dispatch an event message to matching handlers.

        `ran_under`, when given, receives the id the message was dispatched under as soon as it is
        resolved, for a caller that ends the dispatch itself and has to say which id it ran under:
        a handler that is cancelled raises nothing this method can attach the id to.
        """
        subject = msg.subject
        outcome = DispatchOutcome.OK

        if pattern is not None:
            handler = self.registry.event_handlers.get(pattern)
            matching_handlers = [(pattern, handler)] if handler is not None else []
        else:
            matching_handlers = []
            for matched_pattern, handler in self.registry.event_handlers.items():
                if subject_matches(matched_pattern, subject):
                    matching_handlers.append((matched_pattern, handler))

        if not matching_handlers:
            return DispatchOutcome.NO_HANDLER

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
            if isinstance(e, ImportError):
                # The service cannot read this encoding at all: the optional package is not
                # installed. That is a fact about this replica and not a judgement of the message,
                # which a replica with the package would process (the RPC path calls it the
                # service's own fault too). So it is neither dead-lettered, where the body would be
                # stored as replacement characters, nor terminated. On JetStream it is raised and
                # takes the path a handler failure takes: a nak, a redelivery, and the dead letter
                # at the delivery limit. Elsewhere nothing redelivers, and the log line is the trace.
                if raise_on_error:
                    raise
                return DispatchOutcome.INVALID
            await self.dlq.dead_letter_decode_error(msg, e)
            return DispatchOutcome.INVALID

        is_enveloped = (
            isinstance(data, dict)
            and "data" in data
            and "source_service" in data
            and "timestamp" in data
        )
        domain_payload = data["data"] if is_enveloped else data

        # One id for the message, so the handlers it reaches log under the same one.
        correlation_id = CorrelationContext.for_message(headers, data)
        if ran_under is not None:
            ran_under.append(correlation_id)
        for matched_pattern, handler in matching_handlers:
            ctx = WorkerContext(
                kind="event",
                subject=subject,
                headers=headers,
                correlation_id=correlation_id,
                payload=data,
                raw=msg,
            )
            if is_enveloped:
                ctx.data["envelope"] = data
            handler_name = self.registry.event_handler_names.get(matched_pattern) or getattr(
                handler, "__name__", None
            )
            if handler_name:
                ctx.data["handler_name"] = handler_name

            async def call(
                handler: Any = handler,
                data: Any = data,
                domain_payload: Any = domain_payload,
                ctx: Any = ctx,
                matched_pattern: str = matched_pattern,
            ) -> Any:
                nonlocal outcome
                self.logger.info(f"Event {subject} with correlation_id: {ctx.correlation_id}")

                meta = self._get_handler_meta(handler)

                # Validated listeners first: a subject declared with a schema is judged
                # by that schema, whatever else the same method declares.
                schema_entry = self.registry.event_schemas.get(matched_pattern)
                if schema_entry is not None:
                    schema, on_invalid = schema_entry
                    validated_spec = self.registry.event_specs_by_subject.get(matched_pattern)
                    payload_name = (
                        validated_spec.single_model_param_name or "message"
                        if validated_spec is not None
                        else "message"
                    )
                    try:
                        model = validate_payload(
                            schema, _the_model_in(domain_payload, payload_name, schema)
                        )
                        model = with_correlation_id(model, ctx.correlation_id)
                    except Exception as ve:
                        # The phase decides, not the exception type: nothing here has run the
                        # handler, so a failure is a property of the message and of the schema,
                        # and a redelivery repeats it.
                        self._report_unexpected_validation_failure(ve, subject, ctx)
                        await self.dlq.handle_invalid_message(
                            subject,
                            data,
                            ve,
                            schema,
                            on_invalid,
                            correlation_id=ctx.correlation_id,
                            msg=msg,
                        )
                        outcome = DispatchOutcome.INVALID
                        ctx.data["outcome"] = outcome.value
                        return None

                    validated_kwargs: dict[str, Any] = {payload_name: model}
                    if meta.has_subject:
                        validated_kwargs["subject"] = subject
                    if meta.has_correlation_id:
                        validated_kwargs["correlation_id"] = ctx.correlation_id
                    if meta.is_async:
                        return await handler(**validated_kwargs)
                    return handler(**validated_kwargs)

                # Typed event listeners: validate against EventHandlerSpec before dispatch
                spec = self.registry.event_specs_by_subject.get(matched_pattern)

                if spec is not None:
                    call_kwargs: dict[str, Any]
                    try:
                        if spec.is_single_model_param:
                            model = validate_payload(
                                spec.payload_model,
                                _the_model_in(
                                    domain_payload,
                                    spec.single_model_param_name,
                                    spec.payload_model,
                                ),
                            )
                            model = with_correlation_id(model, ctx.correlation_id)
                            call_kwargs = {spec.single_model_param_name: model}  # type: ignore[dict-item]
                        else:
                            # An object is judged by the step an RPC shares. A payload that is
                            # not one is the one value of a handler with one parameter, and
                            # nothing for a handler with none; for any other handler it is
                            # refused by that same step.
                            if isinstance(domain_payload, Mapping):
                                validated = validate_payload(spec.payload_model, domain_payload)
                            elif domain_payload is None and not spec.payload_model.model_fields:
                                validated = spec.payload_model()
                            elif len(spec.payload_model.model_fields) == 1:
                                field_name = next(iter(spec.payload_model.model_fields))
                                validated = validate_decoded(
                                    spec.payload_model, {field_name: domain_payload}
                                )
                            else:
                                validated = validate_payload(spec.payload_model, domain_payload)
                            # Each parameter as the type it declares, as an RPC's: dumping the
                            # validated model back to data would hand a model over as a `dict`.
                            call_kwargs = {
                                param.name: with_correlation_id(
                                    getattr(validated, param.name), ctx.correlation_id
                                )
                                for param in spec.params
                            }
                    except Exception as ve:
                        # As for a validated listener: everything up to the handler's entry is
                        # the message being judged, whatever exception the judging raised.
                        self._report_unexpected_validation_failure(ve, subject, ctx)
                        await self.dlq.handle_invalid_message(
                            subject,
                            data,
                            ve,
                            spec.payload_model,
                            on_invalid=None,
                            correlation_id=ctx.correlation_id,
                            msg=msg,
                        )
                        # Reporting INVALID is the whole of it: the caller owns
                        # the terminal acknowledgement, because it is the layer
                        # that knows whether this delivery can be redelivered.
                        # Terminating here too sends a second one, which a real
                        # client refuses and safe_term swallows into a warning.
                        outcome = DispatchOutcome.INVALID
                        ctx.data["outcome"] = outcome.value
                        return None

                    if spec.takes_subject:
                        call_kwargs["subject"] = subject
                    if spec.takes_correlation_id:
                        call_kwargs["correlation_id"] = ctx.correlation_id

                    if inspect.iscoroutinefunction(handler):
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
            except RetryMessage as e:
                if raise_on_error:
                    carry_correlation_id(e, ctx.correlation_id)
                    raise
                self.logger.warning(
                    f"event {subject} deferred by an extension "
                    f"(correlation_id: {ctx.correlation_id}): {e}"
                )
            except RejectMessage as e:
                # A refusal an extension AUTHORED is a policy decision, and
                # ADR-0006 answers it by acknowledging: the message was seen,
                # judged and turned away, and redelivering it changes nothing.
                #
                # One the PIPELINE synthesised because a `fails_closed` hook
                # raised is the service being broken, not the caller being
                # turned away -- `RejectMessage.hook_crash` says which, and
                # `rpc.py` reads the same flag at both of its `refused:` arms.
                # Acknowledging that one destroys a message the stream exists to
                # keep, for a fault that is ours and that a redelivery may well
                # clear. So it takes the path a handler exception takes: the
                # caller that asked for errors gets one, and on JetStream that
                # means nak, then the DLQ and term once `max_deliver` is spent.
                if e.hook_crash:
                    if raise_on_error:
                        carry_correlation_id(e, ctx.correlation_id)
                        raise
                    self.logger.error(
                        f"event {subject} dropped: an extension hook crashed "
                        f"(correlation_id: {ctx.correlation_id}): {e}"
                    )
                else:
                    self.logger.warning(
                        f"event {subject} refused by an extension "
                        f"(correlation_id: {ctx.correlation_id}): {e}"
                    )
            except Exception as e:
                if raise_on_error:
                    carry_correlation_id(e, ctx.correlation_id)
                    raise
                self.logger.error(
                    f"Error handling event {subject} (correlation_id: {ctx.correlation_id}): {e}"
                )

        return outcome

    # Compatibility aliases
    _get_event_semaphore = get_event_semaphore
    _handle_event = handle_event

"""Synchronous, asynchronous, and introspection RPC dispatch pipeline."""

from __future__ import annotations

import asyncio
import inspect
import json
import traceback
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any

from loguru import logger as global_logger

from ..correlation import CorrelationContext
from ..deadline import Deadline, on_arrival, scoped
from ..error_text import may_expose
from ..extension import RejectMessage, WorkerContext, is_a_finite_delay
from ..messages import with_correlation_id
from ..registry import ServiceRegistry
from ..service_config import ServiceConfig
from ..validation import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_MSGPACK,
    deserialize_payload,
    serialize_payload,
)
from ..validation_extension import redacts_rpc_validation
from . import rpc_stream
from .describe import DescribeAnswers
from .handler_limits import (
    HandlerLimits,
    admit_to_method,
    answer_queue_full,
    not_started,
    refuse_when_queue_full,
    release,
    take,
)
from .pipeline import ExtensionPipeline
from .replies import answer
from .replies import reply_headers as reply_headers  # callers import it from here
from .rpc_limits import (
    admission_bound,
    admit,
    answer_busy,
    deadline_reply,
    deadline_text,
    warn_if_still_running,
)

#: `handle_rpc_request`'s default: the request's deadline is read from it when dispatch reaches it.
_ON_ARRIVAL: Any = object()


class RpcDispatcher:
    """Inbound synchronous RPC, fire-and-forget async RPC, and describe handler.

    Invariants:
    - Bounded concurrency enforced via asyncio.Semaphore if configured.
    - RPC errors formatted into structured JSON envelopes with timestamp and correlation_id.
    - Inbound requests executed through ExtensionPipeline.run_worker.
    """

    def __init__(
        self,
        registry: ServiceRegistry,
        config: ServiceConfig,
        pipeline: ExtensionPipeline,
        task_spawner: Callable[[Coroutine[Any, Any, Any], str | None], asyncio.Task[Any]]
        | None = None,
        logger: Any = None,
        service: Any = None,
        is_stopping: Callable[[], bool] | None = None,
        limits: HandlerLimits | None = None,
        connection: Callable[[], Any] | None = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.pipeline = pipeline
        self.task_spawner = task_spawner
        self.logger = logger or global_logger.bind(service=config.name)
        self.service = service

        self._rpc_semaphore: asyncio.Semaphore | None = None
        self._async_rpc_semaphore: asyncio.Semaphore | None = None
        # Whether the service is stopping or stopped: a request that has waited for a permit is
        # not started then.
        self._is_stopping = is_stopping or (lambda: False)
        # Requests admitted and not yet finished, per path, counted only while a bound applies.
        self._admitted = {"rpc": 0, "async": 0}
        self._describe = DescribeAnswers(self)
        self.limits = limits if limits is not None else HandlerLimits()
        # Replies streamed on the service's connection, by a handler that yields them.
        self._streams = rpc_stream.Streams(connection, config, self.logger) if connection else None

    def _spawn_task(
        self, coro: Coroutine[Any, Any, Any], name: str | None = None
    ) -> asyncio.Task[Any]:
        if self.task_spawner is not None:
            return self.task_spawner(coro, name)
        return asyncio.create_task(coro, name=name)

    def get_rpc_semaphore(self) -> asyncio.Semaphore | None:
        """Get or initialize the RPC concurrency semaphore."""
        limit = self.config.max_rpc_concurrency
        if self._rpc_semaphore is None and limit is not None and limit > 0:
            self._rpc_semaphore = asyncio.Semaphore(limit)
        return self._rpc_semaphore

    def get_async_rpc_semaphore(self) -> asyncio.Semaphore | None:
        """Get or initialize the async RPC concurrency semaphore."""
        limit = self.config.max_async_rpc_concurrency
        if limit is None:
            limit = self.config.max_rpc_concurrency
        if self._async_rpc_semaphore is None and limit is not None and limit > 0:
            self._async_rpc_semaphore = asyncio.Semaphore(limit)
        return self._async_rpc_semaphore

    async def on_rpc_request(self, msg: Any) -> None:
        """Handle incoming NATS RPC subscription message: fix its deadline, admit it, spawn it.

        Nothing here waits. One subscription carries every method's requests and nats-py runs its
        callbacks one at a time, so a callback that waited for a concurrency permit held every
        later request, for every method, in the client's queue. The request's deadline is fixed
        here, on arrival; the permit is waited for on the spawned task, charged to that deadline.
        Over the admission bound (`max_rpc_in_flight`) the request is answered with code `busy`
        and not spawned.
        """
        deadline = self._deadline_of(msg, caller=True)
        sem, held = self.get_rpc_semaphore(), self._limit_of(msg)
        bound = admission_bound(self.config)
        full = refuse_when_queue_full(held)
        if full is not None:
            self._spawn_task(answer_queue_full(msg, held, full, self.logger), name="rpc_busy")
            return
        if bound is not None and self._admitted["rpc"] >= bound:
            handler_name = msg.subject.split(".")[-1]
            text = (
                f"{handler_name} not admitted: {self._admitted['rpc']} requests are already in "
                f"flight, the most this service takes"
            )
            self.logger.warning(text)
            if getattr(msg, "reply", True):
                self._spawn_task(
                    answer_busy(
                        msg, text, self.logger, limit=bound, in_flight=self._admitted["rpc"]
                    ),
                    name="rpc_busy",
                )
            return
        if sem is None and held is None:
            task = self._spawn_task(
                self.handle_rpc_request(msg, deadline=deadline),
                name="rpc_request",
            )
        else:
            task = self._spawn_task(
                self._bounded_handle_rpc(msg, deadline),
                name="rpc_bounded_request",
            )
        if bound is not None:
            admit(self._admitted, task, "rpc")
        admit_to_method(held, task)

    async def _bounded_handle_rpc(self, msg: Any, deadline: Deadline | None = None) -> None:
        """Wait for the handler's permit, then the service's, both by the deadline; dispatch.

        A request whose deadline passes first is answered `deadline_exceeded` and not run; one
        that gets its permits while the service is stopping is answered `busy` and not started.
        """
        try:
            sem, held = self.get_rpc_semaphore(), self._limit_of(msg)
            if not await take(held, sem, deadline):
                await self.handle_rpc_request(msg, deadline=deadline)
                return
            try:
                if self._is_stopping():
                    not_started(held)
                    handler_name = msg.subject.split(".")[-1]
                    text = f"{handler_name} not started: the service is stopping"
                    self.logger.info(text)
                    if getattr(msg, "reply", True):
                        await answer_busy(msg, text, self.logger)
                    return
                await self.handle_rpc_request(msg, deadline=deadline)
            finally:
                release(held, sem)
        except Exception as e:
            # `handle_rpc_request` answers its own failures, so what reaches here escaped its
            # guards (a reply that could not be sent, a bug in dispatch): worth more than DEBUG.
            self.logger.error(f"RPC request failed: {type(e).__name__}: {e}")

    async def on_describe_request(self, msg: Any) -> None:
        """Handle incoming NATS describe subscription message."""
        self._spawn_task(
            self.handle_describe_request(msg),
            name="describe_request",
        )

    async def on_async_request(self, msg: Any) -> None:
        """Handle incoming NATS fire-and-forget async RPC subscription message: admit, spawn.

        Nothing here waits, as for a request that waits for a reply. No caller waits for this one,
        so only `max_rpc_processing_time` bounds it, and a request over the admission bound, or
        one whose time runs out while it waits for a permit, is dropped and logged.
        """
        deadline = self._deadline_of(msg, caller=False)
        sem, held = self.get_async_rpc_semaphore(), self._limit_of(msg)
        bound = admission_bound(self.config)
        full = refuse_when_queue_full(held)
        if full is not None:
            self.logger.warning(f"{full}; the fire-and-forget request is dropped")
            return
        if bound is not None and self._admitted["async"] >= bound:
            self.logger.warning(
                f"{msg.subject.split('.')[-1]} not admitted: {self._admitted['async']} "
                f"fire-and-forget requests are already in flight, the most this service takes; "
                f"the request is dropped"
            )
            return
        if sem is None and held is None:
            task = self._spawn_task(
                self.handle_async_request(msg, deadline=deadline),
                name="async_rpc_request",
            )
        else:
            task = self._spawn_task(
                self._bounded_handle_async_rpc(msg, deadline),
                name="async_rpc_bounded_request",
            )
        if bound is not None:
            admit(self._admitted, task, "async")
        admit_to_method(held, task)

    async def _bounded_handle_async_rpc(self, msg: Any, deadline: Deadline | None = None) -> None:
        """Wait for the handler's permit, then the service's, both by the deadline; dispatch."""
        sem, held = self.get_async_rpc_semaphore(), self._limit_of(msg)
        if not await take(held, sem, deadline):
            assert deadline is not None
            self.logger.warning(deadline_text(msg.subject.split(".")[-1], deadline))
            return
        try:
            if self._is_stopping():
                not_started(held)
                self.logger.info(
                    f"{msg.subject.split('.')[-1]} not started: the service is stopping, and the "
                    f"fire-and-forget request is dropped"
                )
                return
            await self.handle_async_request(msg, deadline=deadline)
        finally:
            release(held, sem)

    def _limit_of(self, msg: Any) -> Any:
        """The concurrency limit of the method `msg` calls, or None."""
        return self.limits.of(self.registry.rpc_handlers.get(msg.subject.split(".")[-1]))

    def _deadline_of(self, msg: Any, *, caller: bool) -> Deadline | None:
        """The deadline of a request arriving now: its caller's budget, if `caller` and it sent
        one, or the service's `max_rpc_processing_time`, whichever ends first."""
        msg_h = getattr(msg, "headers", None)
        headers = msg_h if caller and isinstance(msg_h, Mapping) else {}
        return on_arrival(headers, self.config.max_rpc_processing_time)

    async def _run_worker(self, ctx: WorkerContext, call: Callable[[], Awaitable[Any]]) -> Any:
        return await self.pipeline.run_worker(ctx, call)

    def _what_a_refusal_adds(self, refusal: BaseException) -> dict[str, Any]:
        """The fields a refusal that knows more than its reason adds to the reply.

        ``retry_after`` (seconds) when the refusal carries a finite number of zero or more, as a
        ``RetryMessage`` does, and ``details`` when it carries a non-empty dict. A ``nan``, an
        ``inf`` or a negative number is not written: ``NaN`` and ``Infinity`` are not JSON, and a
        wait of less than nothing says nothing, so the reply is the one a refusal with no
        ``retry_after`` gets. Both are additions: a refusal with
        neither gets exactly the reply it always got, and the three fields a caller parses are
        untouched. They are extras, so they never cost the caller the refusal: values that cannot
        be written are stringified, and a ``details`` that cannot be written at all is dropped
        and logged, because a reply that fails to serialise is no reply.
        """
        added: dict[str, Any] = {}
        retry_after = getattr(refusal, "retry_after", None)
        if is_a_finite_delay(retry_after) and retry_after >= 0:
            added["retry_after"] = retry_after
        details = getattr(refusal, "details", None)
        if isinstance(details, dict) and details:
            try:
                added["details"] = json.loads(json.dumps(details, default=str))
            except (TypeError, ValueError) as exc:
                self.logger.warning(
                    f"dropping the details of a {type(refusal).__name__} from the refusal "
                    f"reply: they cannot be written ({exc})"
                )
        return added

    async def handle_rpc_request(self, msg: Any, *, deadline: Any = _ON_ARRIVAL) -> None:
        """Execute RPC dispatch pipeline, validating input and replying with an envelope.

        The handler is bounded by `deadline` (the earlier of its caller's budget and
        `max_rpc_processing_time`), read from the request when dispatch reaches it unless the
        caller of this method fixed it on arrival. A request already past it is answered with
        code `deadline_exceeded` and not run; one that runs past it is cancelled and answered
        the same way, and its late result, if it suppressed the cancellation, is not sent.
        """
        if deadline is _ON_ARRIVAL:
            deadline = self._deadline_of(msg, caller=True)
        subject = msg.subject
        handler_name = subject.split(".")[-1]
        has_reply = bool(getattr(msg, "reply", True))

        msg_h = getattr(msg, "headers", None)
        headers = dict(msg_h) if isinstance(msg_h, Mapping) else {}
        content_type = None
        for k, v in headers.items():
            if k.lower() == "content-type":
                content_type = v.split(";")[0].strip().lower()
                break

        reply_format = self.config.serialization_format
        if content_type == CONTENT_TYPE_MSGPACK:
            reply_format = "msgpack"
        elif content_type == CONTENT_TYPE_JSON:
            reply_format = "json"
        elif not content_type:
            if msg.data and msg.data.lstrip().startswith((b"{", b"[")):
                reply_format = "json"

        stream: rpc_stream.StreamedRequest | None = None

        async def _send(resp_data: dict[str, Any], fmt: str) -> None:
            # The envelope that ends a stream says how many chunks it sent.
            resp_data, ends = stream.ending(resp_data) if stream else (resp_data, None)
            resp_bytes, resp_ct = serialize_payload(resp_data, format=fmt)
            await answer(
                msg,
                resp_bytes,
                content_type=resp_ct,
                correlation_id=resp_data.get("correlation_id"),
                extra_headers=ends,
            )

        async def _respond(resp_data: dict[str, Any]) -> None:
            """Answer the caller, in JSON if the requested format cannot be written.

            `reply_format` comes from the REQUEST's content type, so a service
            that never configured msgpack still tries to answer a msgpack
            request in msgpack. Without the optional extra installed that raises
            inside `serialize_payload`, and this used to swallow it at DEBUG --
            so the caller received nothing at all and blocked until its own
            timeout, with one debug line on the server as the only record.

            JSON is always available (it is the fallback `deserialize_payload`
            already uses), and the reply carries its real content type, which is
            what the client reads to decode it. A caller that asked for msgpack
            and gets readable JSON is strictly better off than one that gets
            silence.
            """
            try:
                await _send(resp_data, reply_format)
                return
            except Exception as preferred_failed:
                if reply_format == "json":
                    self.logger.error(f"Failed to send RPC reply: {preferred_failed}")
                    return
                self.logger.error(
                    f"Cannot write a {reply_format} reply to {handler_name} "
                    f"({preferred_failed}); answering in JSON instead"
                )
            try:
                await _send(resp_data, "json")
            except Exception as json_failed:  # pragma: no cover - nothing left to try
                self.logger.error(f"Failed to send RPC reply: {json_failed}")

        if handler_name not in self.registry.rpc_handlers:
            if has_reply:
                cid = CorrelationContext.extract_from_headers(headers)
                error_response: dict[str, Any] = {
                    "success": False,
                    "error": f"Unknown method: {handler_name}",
                    "code": "unknown_method",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "correlation_id": cid,
                }
                await _respond(error_response)
            return

        handler = self.registry.rpc_handlers[handler_name]
        spec = self.registry.rpc_specs[handler_name]

        cid = CorrelationContext.extract_from_headers(headers)
        if (refusal := rpc_stream.mismatch(spec.streams, headers, handler_name, cid)) is not None:
            if has_reply:
                await _respond(refusal)
            return
        if spec.streams and self._streams is not None:
            stream = self._streams.open(spec.return_adapter, reply_format, handler_name)

        if deadline is not None and deadline.remaining() <= 0:
            self.logger.warning(deadline_text(handler_name, deadline))
            if has_reply:
                cid = CorrelationContext.extract_from_headers(headers)
                await _respond(deadline_reply(handler_name, deadline, cid, ran=False))
            return

        try:
            data = deserialize_payload(
                msg.data,
                content_type=content_type,
                fallback_format=self.config.serialization_format,
            )
        except Exception as e:
            diagnostic = (
                "invalid encoded payload" if redacts_rpc_validation(self.config) else str(e)
            )
            self.logger.error(
                f"Error decoding payload for RPC request {handler_name}: {diagnostic}"
            )
            if has_reply:
                cid = CorrelationContext.extract_from_headers(headers)
                if isinstance(e, ImportError):
                    # The service cannot read this encoding at all -- the
                    # optional extra is not installed. That is the service's
                    # own fault and not the caller's arguments, so it must not
                    # arrive as "validation failed": a caller told that would
                    # go looking at its payload, which is correct.
                    await _respond(
                        {
                            "success": False,
                            "error": f"unsupported serialization format: {diagnostic}",
                            "code": "internal",
                            "timestamp": datetime.now(UTC).isoformat(),
                            "correlation_id": cid,
                        }
                    )
                    return
                error_response = {
                    "success": False,
                    "error": "validation failed",
                    "code": "validation_failed",
                    "details": [
                        {
                            "loc": ["__root__"],
                            "msg": f"Invalid payload: {diagnostic}",
                            "type": "payload_invalid",
                        }
                    ],
                    "timestamp": datetime.now(UTC).isoformat(),
                    "correlation_id": cid,
                }
                await _respond(error_response)
            return

        ctx = WorkerContext(
            kind="rpc",
            subject=subject,
            headers=headers,
            correlation_id=None,
            payload=data,
            raw=msg,
        )
        ctx.data["handler_name"] = handler_name

        async def call() -> Any:
            self.logger.info(
                f"RPC request {handler_name} with correlation_id: {ctx.correlation_id}"
            )
            raw_kwargs = ctx.data.get("validated_kwargs")
            if raw_kwargs is None:
                raw_kwargs = ctx.payload if isinstance(ctx.payload, dict) else {}
            kwargs = {
                name: with_correlation_id(value, ctx.correlation_id)
                for name, value in raw_kwargs.items()
            }
            if spec.takes_correlation_id:
                kwargs["correlation_id"] = ctx.correlation_id
            if stream is not None:
                # Each item is published as it is yielded, inside this call, so the deadline, the
                # permits and the admission slot bound the whole stream.
                return await stream.send(handler(**kwargs), msg.reply, ctx.correlation_id)
            if inspect.iscoroutinefunction(handler):
                result = await handler(**kwargs)
            else:
                result = handler(**kwargs)
            result = with_correlation_id(result, ctx.correlation_id)
            return spec.return_adapter.dump_python(
                spec.return_adapter.validate_python(result), mode="json"
            )

        bound = asyncio.timeout_at(None if deadline is None else deadline.at)
        still_running = warn_if_still_running(self.logger, handler_name, deadline)
        try:
            try:
                with scoped(deadline):
                    async with bound:
                        result = await self._run_worker(ctx, call)
            except TimeoutError:
                if not bound.expired():
                    raise
            finally:
                if still_running is not None:
                    still_running.cancel()
        except RejectMessage as e:
            if has_reply:
                error = ctx.data.get("validation_error")
                if error is not None:
                    response: dict[str, Any] = {
                        "success": False,
                        "error": "validation failed",
                        "code": "validation_failed",
                        "details": json.loads(error.json()),
                        "timestamp": datetime.now(UTC).isoformat(),
                        "correlation_id": ctx.correlation_id,
                    }
                else:
                    # A `RejectMessage` an extension AUTHORED is a refusal and
                    # says so. One the pipeline synthesised because a
                    # fails_closed hook crashed is the service being broken, and
                    # a caller told "refused" would go looking at its own
                    # credentials for a fault that is ours. The pipeline states
                    # which at the raise site; this reads the statement.
                    crashed = e.hook_crash
                    response = {
                        "success": False,
                        "error": str(e) if crashed else f"refused: {e}",
                        "code": "internal" if crashed else "refused",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "correlation_id": ctx.correlation_id,
                    }
                    if not crashed:
                        response.update(self._what_a_refusal_adds(e))
                await _respond(response)
            return
        except Exception as e:
            self.logger.exception(
                f"Error handling RPC request {handler_name} "
                f"(correlation_id: {ctx.correlation_id}): {e}"
            )
            if has_reply:
                if may_expose(self.config):
                    error_response = {
                        "success": False,
                        "error": str(e) or e.__class__.__name__,
                        "code": "internal",
                        "traceback": traceback.format_exc(),
                        "timestamp": datetime.now(UTC).isoformat(),
                        "correlation_id": ctx.correlation_id,
                    }
                else:
                    error_response = {
                        "success": False,
                        "error": f"Internal server error (correlation_id: {ctx.correlation_id})",
                        "code": "internal",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "correlation_id": ctx.correlation_id,
                    }
                await _respond(error_response)
            return

        if bound.expired():
            assert deadline is not None
            self.logger.warning(deadline_text(handler_name, deadline, ran=True))
            if has_reply:
                await _respond(deadline_reply(handler_name, deadline, ctx.correlation_id, ran=True))
            return

        if isinstance(result, rpc_stream.StreamEnded) and not result.complete:
            if has_reply and result.refusal is not None:
                await _respond(result.refusal)
            return

        if has_reply:
            response = {
                "success": True,
                "result": None if isinstance(result, rpc_stream.StreamEnded) else result,
                "timestamp": datetime.now(UTC).isoformat(),
                "correlation_id": ctx.correlation_id,
            }
            await _respond(response)

    async def handle_describe_request(self, msg: Any) -> None:
        """Answer this service's Description metadata in canonical bytes."""
        await self._describe.answer(msg)

    async def handle_async_request(self, msg: Any, *, deadline: Any = _ON_ARRIVAL) -> None:
        """Handle incoming fire-and-forget async RPC requests, bounded by
        `max_rpc_processing_time` when it is set: a handler that runs past it is cancelled and
        logged."""
        if deadline is _ON_ARRIVAL:
            deadline = self._deadline_of(msg, caller=False)
        subject = msg.subject
        handler_name = subject.split(".")[-1]

        if handler_name not in self.registry.rpc_handlers:
            self.logger.warning(f"Unknown async method: {handler_name}")
            return

        handler = self.registry.rpc_handlers[handler_name]
        spec = self.registry.rpc_specs[handler_name]
        if spec.streams:
            self.logger.warning(rpc_stream.NOT_RUN_ASYNC.format(handler_name))
            return

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
            diagnostic = (
                "invalid encoded payload" if redacts_rpc_validation(self.config) else str(e)
            )
            self.logger.error(
                f"Error decoding payload for async request {handler_name}: {diagnostic}"
            )
            return

        ctx = WorkerContext(
            kind="async_rpc",
            subject=subject,
            headers=headers,
            correlation_id=None,
            payload=data,
            raw=msg,
        )
        ctx.data["handler_name"] = handler_name

        async def call() -> Any:
            raw_kwargs = ctx.data.get("validated_kwargs")
            if raw_kwargs is None:
                raw_kwargs = ctx.payload if isinstance(ctx.payload, dict) else {}
            kwargs = {
                name: with_correlation_id(value, ctx.correlation_id)
                for name, value in raw_kwargs.items()
            }
            if spec.takes_correlation_id:
                kwargs["correlation_id"] = ctx.correlation_id
            self.logger.info(
                f"Async request {handler_name} with correlation_id: {ctx.correlation_id}"
            )
            if inspect.iscoroutinefunction(handler):
                result = await handler(**kwargs)
            else:
                result = handler(**kwargs)
            # No reply carries it, but `worker_result` hooks see it, as they see
            # a sync result -- which is filled above -- so the two agree.
            return with_correlation_id(result, ctx.correlation_id)

        bound = asyncio.timeout_at(None if deadline is None else deadline.at)
        still_running = warn_if_still_running(self.logger, handler_name, deadline)
        try:
            try:
                with scoped(deadline):
                    async with bound:
                        await self._run_worker(ctx, call)
            except TimeoutError:
                if not bound.expired():
                    raise
            finally:
                if still_running is not None:
                    still_running.cancel()
            if bound.expired():
                assert deadline is not None
                self.logger.warning(deadline_text(handler_name, deadline, ran=True))
        except RejectMessage as e:
            error = ctx.data.get("validation_error")
            if e.hook_crash:
                # A gate that crashed is a fault in the service, not a request turned away.
                self.logger.error(
                    f"Async request {handler_name} not run: an extension hook crashed "
                    f"(correlation_id: {ctx.correlation_id}): {e}"
                )
            elif error is not None:
                self.logger.warning(
                    f"Async request {handler_name} failed validation "
                    f"(correlation_id: {ctx.correlation_id}): {error.errors()}"
                )
            else:
                self.logger.warning(
                    f"Async request {handler_name} refused "
                    f"(correlation_id: {ctx.correlation_id}): {e}"
                )
        except Exception as e:
            self.logger.error(
                f"Error handling async request {handler_name} "
                f"(correlation_id: {ctx.correlation_id}): {e}"
            )

    # Compatibility aliases
    _get_rpc_semaphore = get_rpc_semaphore
    _get_async_rpc_semaphore = get_async_rpc_semaphore
    _handle_rpc_request = handle_rpc_request
    _handle_describe_request = handle_describe_request
    _handle_async_request = handle_async_request

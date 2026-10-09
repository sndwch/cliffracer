"""Structured logging configuration using loguru."""

import asyncio
import functools
import json
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, cast

from loguru import logger

from cliffracer.core.credentials import is_credential_name
from cliffracer.core.decorators import refuse_bare_use
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.service_config import ServiceConfig

from ._log_dir import log_file_stem, resolve_log_dir
from ._service_stamp import claim_process_service, is_bound_service
from ._template import escape_braces
from .correlation_logging import attach_correlation_id

LogRecordRedactor = Callable[[dict[str, Any]], dict[str, Any]]


def _redact_sensitive_values(value: Any, credential_names: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if is_credential_name(key, credential_names)
                else _redact_sensitive_values(item, credential_names)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_values(item, credential_names) for item in value]
    return value


def redact_sensitive_log_fields(
    record: dict[str, Any], *, credential_names: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Replace values under credential keys throughout a log record.

    A key is a credential by the rule the dead-letter publisher applies to message headers
    (`cliffracer.core.credentials.is_credential_name`), and also when it is one of
    `credential_names`: the headers the installed extensions read a credential from, which
    `LoggingExtension` passes. The NATS sink passes a freshly decoded record, so a redactor may
    mutate and return it without changing records delivered to the process's other sinks.
    """
    return cast(dict[str, Any], _redact_sensitive_values(record, credential_names))


DEFAULT_NATS_SINK_MAX_PENDING = 1000
"""Publishes a NATS log sink lets be in flight before it drops records."""


@dataclass
class NatsSinkStats:
    """What a NATS log sink did with the records it was handed.

    Updated from loguru's writer thread and from the event loop, so every change
    holds one lock. ``published`` counts publishes the NATS client accepted, not
    deliveries.

    - ``published``: records the client accepted.
    - ``failed``: records the sink could not build or the client refused.
    - ``dropped``: records never scheduled because the backlog was full or the
      loop that publishes them was gone, plus publishes cancelled before they ran.
    - ``pending``: publishes scheduled and not yet finished.
    - ``last_error``: the exception type name of the latest loss, else ``None``.
    """

    published: int = 0
    failed: int = 0
    dropped: int = 0
    pending: int = 0
    last_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _last_said: str | None = field(default=None, repr=False, compare=False)

    def snapshot(self) -> dict[str, Any]:
        """The counters as plain data, read at one moment."""
        with self._lock:
            return {
                "published": self.published,
                "failed": self.failed,
                "dropped": self.dropped,
                "pending": self.pending,
                "last_error": self.last_error,
            }

    def _lost(self, counter: str, error: str) -> bool:
        """Count one lost record; True when this error was not the last one reported."""
        with self._lock:
            setattr(self, counter, getattr(self, counter) + 1)
            self.last_error = error
            new, self._last_said = error != self._last_said, error
            return new

    def _delivered(self) -> None:
        with self._lock:
            self.published += 1
            self._last_said = None

    def _admit(self, limit: int) -> bool:
        """Reserve one in-flight publish, or refuse when ``limit`` are already out."""
        with self._lock:
            if self.pending >= limit:
                return False
            self.pending += 1
            return True

    def _finished(self) -> None:
        with self._lock:
            self.pending -= 1


def _say_once(stats: NatsSinkStats, counter: str, error: str, what: str) -> None:
    """Count a loss, and write it to stderr when it is a different error from the last one.

    Every further loss is counted and visible through the stats; a sink that fails on
    every line would otherwise cost one stderr write per log line. A publish that
    succeeds clears the memory, so the next failure is written again.
    """
    if stats._lost(counter, error):
        sys.stderr.write(
            f"NATS log sink {what}: {error} (further losses are counted, not printed)\n"
        )


class LoggingConfig:
    """Centralized logging configuration using loguru"""

    @staticmethod
    def configure(
        service_name: str,
        log_level: str = "INFO",
        log_dir: str | None = None,
        structured: bool = True,
        enable_console: bool = True,
        enable_file: bool = True,
        rotation: str = "10 MB",
        retention: str = "1 week",
        compression: str = "gz",
        replace_existing: bool = True,
    ) -> list[int]:
        """
        Configure structured logging for a service

        Args:
            service_name: Name of the service for log identification
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            log_dir: Directory for log files (defaults to $CLIFFRACER_LOG_DIR, else ./logs)
            structured: Whether to use structured JSON logging
            enable_console: Whether to log to console
            enable_file: Whether to log to file
            rotation: Log rotation policy
            retention: Log retention policy
            compression: Log compression format
            replace_existing: Remove every sink loguru held before this call, the host's
                own and those another service configured, once this service's sinks
                are installed, and make `service_name` the process-wide `service`. False
                adds this service's sinks next to what is already installed and keeps the
                process-wide `service` another service named, with one WARNING naming both
                (a `service_name` nothing has named yet is set).

        Returns:
            The ids of the sinks this call added, so a caller can remove just those
            with ``logger.remove(id)``.

        Raises whatever loguru raises for a sink it cannot open (``PermissionError``
        for a log directory that cannot be written, ``ValueError`` for a rotation,
        retention or compression it refuses). Nothing has been removed or left behind
        when it does: the sinks that were already in place are still in place, and the
        ones this call had added are gone.

        ``service`` is merged into the process's global extra, so context the host
        application set (app, region, version) stays on every record. Loguru's extra
        is process-wide, so records written through the plain ``logger`` carry the
        service configured last; a ``ContextualLogger`` labels its own lines.
        """
        log_path = resolve_log_dir(log_dir)
        # Created before any handler is removed: a directory that cannot be
        # created raises with the process's logging still in place.
        if enable_file:
            log_path.mkdir(parents=True, exist_ok=True)

        # Configure formats
        if structured:
            # In structured mode, serialize=True formats records as JSON.
            console_format = "{message}"
            file_format = "{message}"
        else:
            # Human-readable format for development
            console_format = (
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                f"<magenta>{escape_braces(service_name)}</magenta> | "
                "<level>{message}</level>"
            )
            file_format = (
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
                "{level: <8} | "
                "{name}:{function}:{line} | "
                f"{escape_braces(service_name)} | "
                "{message}"
            )

        sinks: list[tuple[Any, dict[str, Any]]] = []

        # Console logging
        if enable_console:
            sinks.append(
                (
                    sys.stderr,
                    {
                        "format": console_format,
                        "level": log_level,
                        "colorize": not structured,
                        "serialize": structured,
                        "enqueue": True,  # Critical for async safety
                    },
                )
            )

        # File logging
        if enable_file:
            file_options: dict[str, Any] = {
                "format": file_format,
                "rotation": rotation,
                "retention": retention,
                "compression": compression,
                "serialize": structured,
                "enqueue": True,  # Async safety
            }
            # Main log file
            sinks.append(
                (
                    escape_braces(str(log_path / f"{log_file_stem(service_name)}.log")),
                    {**file_options, "level": log_level},
                )
            )
            # Error-only log file
            sinks.append(
                (
                    escape_braces(str(log_path / f"{log_file_stem(service_name)}_errors.log")),
                    {**file_options, "level": "ERROR"},
                )
            )

        # The sinks go in BEFORE anything is removed. A sink that fails to open (a directory
        # that exists but cannot be written, a rotation loguru refuses) must leave the process
        # logging as it was, not with no handler at all; a failure takes out only what this
        # call added, whatever raised.
        previous_ids = tuple(logger._core.handlers)  # type: ignore[attr-defined]
        handler_ids: list[int] = []
        try:
            for sink, options in sinks:
                handler_ids.append(logger.add(sink, **options))
        except BaseException:
            for added in handler_ids:
                with suppress(ValueError):
                    logger.remove(added)
            raise
        if replace_existing:
            for previous in previous_ids:
                with suppress(ValueError):  # already removed by someone else since the snapshot
                    logger.remove(previous)

        claim_process_service(
            service_name,
            replace_existing=replace_existing,
            what="Logging",
            set_when_unset=True,
        )

        logger.bind(
            log_level=log_level,
            structured=structured,
            log_dir=str(log_path),
        ).info(f"Logging configured for service '{service_name}'")
        return handler_ids

    @staticmethod
    def add_nats_sink(
        service_name: str,
        nats_connection: Any,
        *,
        config: ServiceConfig,
        log_level: str = "INFO",
        redactor: LogRecordRedactor = redact_sensitive_log_fields,
        max_pending: int = DEFAULT_NATS_SINK_MAX_PENDING,
        stats: NatsSinkStats | None = None,
    ) -> int:
        """
        Add NATS log streaming sink after service connects to NATS.
        This allows logs to be published to NATS topics for real-time streaming.

        THE SUBJECT IS BUILT BY THE SAME HELPER THE LISTENERS USE, and `config`
        is required, and keyword-only, for that reason. Positionally it would sit
        where `log_level` used to, so the pre-existing call
        `add_nats_sink("svc", nc, "DEBUG")` bound cleanly with a string as the
        config -- and then every log line failed inside the sink's own
        `except Exception` with `'str' object has no attribute 'namespace'`,
        written to stderr, publishing nothing. A caller lost their whole log
        stream and got no exception. Keyword-only makes that a TypeError at the
        call site instead. This sink published a
        raw `logs.<service>.<level>` while the ingester's `@listener("logs.>")`
        resolved through `effective_event_subject` and received the namespace,
        so a namespaced deployment's logs reached nobody -- the publish
        succeeded and nothing matched. An optional parameter would leave the
        two ends able to disagree again the moment a caller omitted it.

        Args:
            service_name: Name of the service for log routing
            nats_connection: Active NATS connection (nc)
            config: The service's config, for the namespace and environment prefix
            log_level: Minimum level to stream to NATS
            redactor: Transform applied to each decoded record before publication
            max_pending: Publishes allowed in flight at once. A record that arrives
                with that many already out is dropped and counted, because a log
                call must not wait on the broker and an unbounded backlog is
                memory the process does not have.
            stats: Where the sink counts what it published, lost and has in flight;
                pass one to read it. A sink given none counts into one nothing reads.
        """
        if max_pending < 1:
            raise ValueError(f"max_pending must be at least 1, got {max_pending}")
        stats = stats if stats is not None else NatsSinkStats()
        loop = asyncio.get_running_loop()

        in_flight: set[asyncio.Task[None]] = set()

        def nats_sink(message: Any) -> None:
            """
            Sink that publishes logs to NATS safely across threads.
            Logs are published to: logs.<service_name>.<level>. Only records a call bound
            to this service (`extra["service"]`) reach it, not the process-wide stamp `configure` stores.
            """
            try:
                record = json.loads(message)
                level = record["record"]["level"]["name"].lower()
                subject = HandlerDiscovery.with_namespace(config, f"logs.{service_name}.{level}")
                payload = json.dumps(redactor(record), separators=(",", ":")).encode()
            except Exception as e:
                _say_once(stats, "failed", type(e).__name__, "processing failed")
                return

            if loop.is_closed():
                _say_once(stats, "dropped", "EventLoopClosed", "dropped a record")
                return
            if not stats._admit(max_pending):
                _say_once(stats, "dropped", "BacklogFull", "dropped a record")
                return

            async def publish() -> None:
                try:
                    await nats_connection.publish(subject, payload)
                except Exception as e:
                    _say_once(stats, "failed", type(e).__name__, "publish failed")
                else:
                    stats._delivered()

            def start() -> None:
                # Runs on the loop. The coroutine is built here, not on the writer
                # thread, so a loop that closes before this runs never has one
                # created to be warned about.
                task = loop.create_task(publish())
                in_flight.add(task)  # the loop holds tasks weakly
                task.add_done_callback(finished)

            def finished(task: asyncio.Task[None]) -> None:
                in_flight.discard(task)
                stats._finished()
                if task.cancelled():
                    _say_once(stats, "dropped", "Cancelled", "dropped a record")

            try:
                loop.call_soon_threadsafe(start)
            except Exception as e:
                # The loop closed between the check and the schedule.
                stats._finished()
                _say_once(stats, "dropped", type(e).__name__, "dropped a record")

        # Add the NATS sink
        handler_id = logger.add(
            nats_sink,
            serialize=True,  # Always JSON for NATS
            level=log_level,
            # Loguru sinks are process-wide: without this a second streaming service's sink would
            # publish this service's lines under its own name, and a host line under every name.
            # A line nothing bound a service to carries the process-wide stamp `configure` stored,
            # which is not a binding, so it is not streamed.
            filter=lambda record: (
                is_bound_service(record["extra"].get("service"))
                and record["extra"]["service"] == service_name
            ),
            enqueue=True,  # Critical for async safety
        )

        logger.bind(service=service_name, log_level=log_level, handler_id=handler_id).info(
            f"NATS log streaming enabled for service '{service_name}'"
        )

        return handler_id


class ContextualLogger:
    """Logger with contextual information for microservices.

    Every line carries ``service`` set to ``service_name``. A ``service`` in the
    context, or passed on one call, replaces it for those lines. A line reports the
    ``name``, ``function`` and ``line`` of the code that called the logger.
    """

    def __init__(self, service_name: str, context: dict[str, Any] | None = None):
        self.service_name = service_name
        self.context = context or {}
        self._logger = logger.patch(attach_correlation_id).bind(
            **{"service": service_name, **self.context}
        )

    def with_context(self, **kwargs: Any) -> "ContextualLogger":
        """Create a new logger with additional context"""
        new_context = {**self.context, **kwargs}
        return ContextualLogger(self.service_name, new_context)

    def _located_at(self, func: Callable[..., Any]) -> "ContextualLogger":
        """Report ``func`` as the place its lines were written, for a decorator's logger.

        A decorator writes its lines from its own wrapper, which is no more where the handler
        logged than this module is; the decorated function is the place worth naming.
        """

        def locate(record: Any) -> None:
            record["name"] = func.__module__
            record["function"] = func.__name__
            code = getattr(func, "__code__", None)
            if code is not None:
                record["line"] = code.co_firstlineno

        inner = getattr(self, "_logger", None)
        if inner is not None:
            self._logger = inner.patch(locate)
        return self

    def debug(self, message: str, **kwargs: Any) -> None:
        """Log debug message with context"""
        self._logger.bind(**kwargs).opt(depth=1).debug(message)

    def info(self, message: str, **kwargs: Any) -> None:
        """Log info message with context"""
        self._logger.bind(**kwargs).opt(depth=1).info(message)

    def warning(self, message: str, **kwargs: Any) -> None:
        """Log warning message with context"""
        self._logger.bind(**kwargs).opt(depth=1).warning(message)

    def error(self, message: str, **kwargs: Any) -> None:
        """Log error message with context"""
        self._logger.bind(**kwargs).opt(depth=1).error(message)

    def critical(self, message: str, **kwargs: Any) -> None:
        """Log critical message with context"""
        self._logger.bind(**kwargs).opt(depth=1).critical(message)

    def exception(self, message: str, **kwargs: Any) -> None:
        """Log exception with traceback and context"""
        self._logger.bind(**kwargs).opt(depth=1).exception(message)


def get_service_logger(service_name: str, **context: Any) -> "ContextualLogger":
    """Get a contextual logger for a service"""
    return ContextualLogger(service_name, context)


def _caller_service_name(service: Any) -> str:
    """The configured name of the service a handler was called on."""
    cfg = getattr(service, "config", None) if service else None
    if isinstance(cfg, dict):
        return str(cfg.get("name", "unknown"))
    return getattr(cfg, "name", "unknown") if cfg is not None else "unknown"


def _wrap_with_logging(
    func: Callable[..., Any],
    begin: Callable[[tuple[Any, ...], dict[str, Any]], ContextualLogger],
    end: Callable[[ContextualLogger, Any], None],
    fail: Callable[[ContextualLogger, Exception], None],
) -> Callable[..., Any]:
    """Wrap ``func`` so ``begin``, then ``end`` or ``fail``, bracket each call.

    The wrapper carries ``func``'s name, docstring and signature, so handler
    discovery reads the handler's own parameters, and it is a coroutine
    function exactly when ``func`` is.
    """
    if asyncio.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            call_logger = begin(args, kwargs)
            try:
                result = await func(*args, **kwargs)
            except Exception as e:
                fail(call_logger, e)
                raise
            end(call_logger, result)
            return result

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        call_logger = begin(args, kwargs)
        try:
            result = func(*args, **kwargs)
        except Exception as e:
            fail(call_logger, e)
            raise
        end(call_logger, result)
        return result

    return sync_wrapper


# Decorator for automatic request/response logging
def log_rpc_calls(
    logger_instance: ContextualLogger,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to automatically log RPC calls"""
    # Validate decorator argument to prevent bare use.
    refuse_bare_use(logger_instance, "log_rpc_calls", "@log_rpc_calls(logger)")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        def begin(args: tuple[Any, ...], kwargs: dict[str, Any]) -> ContextualLogger:
            # The service instance is usually the first argument.
            service = args[0] if args else None
            request_logger = logger_instance.with_context(
                rpc_method=func.__name__,
                service=_caller_service_name(service),
                request_arg_names=tuple(sorted(kwargs)),
                positional_arg_count=max(len(args) - (1 if service is not None else 0), 0),
            )._located_at(func)
            request_logger.info(f"RPC call started: {func.__name__}")
            return request_logger

        def end(request_logger: ContextualLogger, result: Any) -> None:
            request_logger.info(
                f"RPC call completed: {func.__name__}", result_type=type(result).__name__
            )

        def fail(request_logger: ContextualLogger, error: Exception) -> None:
            request_logger.error(
                f"RPC call failed: {func.__name__}",
                error=str(error),
                error_type=type(error).__name__,
            )

        return _wrap_with_logging(func, begin, end, fail)

    return decorator


def log_event_handling(
    logger_instance: ContextualLogger,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to automatically log event handling"""
    # Same shape as log_rpc_calls above.
    refuse_bare_use(logger_instance, "log_event_handling", "@log_event_handling(logger)")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        def begin(args: tuple[Any, ...], kwargs: dict[str, Any]) -> ContextualLogger:
            service = args[0] if args else None
            event_logger = logger_instance.with_context(
                event_handler=func.__name__,
                service=_caller_service_name(service),
                event_arg_names=tuple(sorted(kwargs)),
                positional_arg_count=max(len(args) - (1 if service is not None else 0), 0),
            )._located_at(func)
            event_logger.debug(f"Event handling started: {func.__name__}")
            return event_logger

        def end(event_logger: ContextualLogger, result: Any) -> None:
            event_logger.debug(f"Event handling completed: {func.__name__}")

        def fail(event_logger: ContextualLogger, error: Exception) -> None:
            event_logger.error(
                f"Event handling failed: {func.__name__}",
                error=str(error),
                error_type=type(error).__name__,
            )

        return _wrap_with_logging(func, begin, end, fail)

    return decorator

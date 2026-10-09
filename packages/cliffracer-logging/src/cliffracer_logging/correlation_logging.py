"""Loguru sink configuration with correlation context.

Formats log records with service name and the ambient correlation ID extracted
from CorrelationContext.
"""

from __future__ import annotations

import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from loguru import logger

from cliffracer.core.correlation import CorrelationContext

from ._log_dir import log_file_stem, resolve_log_dir
from ._service_stamp import ProcessService, claim_process_service
from ._template import escape_braces

if TYPE_CHECKING:
    from loguru import Record


def setup_correlation_logging(
    service_name: str,
    log_level: str = "INFO",
    log_format: str | None = None,
    *,
    log_dir: str | None = None,
    enable_file: bool = True,
    replace_existing: bool = True,
) -> None:
    """
    Configure loguru to include correlation IDs in all log messages.

    The sinks added here fill `correlation_id` and `service` into each record's `extra`, and
    loguru hands every handler the same record, so a handler registered after these sees the two
    keys on every record, whether or not it asked for them. That is how the keys reach the sinks
    that format with them, and it is by registration order: a sink added before this call does not
    see them. A `service` or a `correlation_id` the call bound itself (`logger.bind(service=...)`) is kept.

    Args:
        service_name: Name of the service for log identification
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_format: Custom log format (uses sensible default if not provided)
        log_dir: Directory for the text and JSON log files (defaults to $CLIFFRACER_LOG_DIR,
            else ./logs, as for `LoggingConfig.configure`)
        enable_file: Whether to write the log files; False writes none and creates no directory
        replace_existing: Remove every sink loguru held before this call, the host's own and those
            another service configured, as `LoggingConfig.configure` does by default, and make
            `service_name` the process-wide `service`, so every line carries it. False adds this
            service's sinks next to what is already installed and leaves the process-wide
            `service` alone: if an earlier `configure` or setup named another service, lines keep
            that name, and one WARNING says so.

    A sink that cannot be opened (a log directory that exists but cannot be written) raises with
    the process's logging as it was found: the new sinks go in first, and the sinks that were
    there are removed once every new one is in.
    """
    log_path = resolve_log_dir(log_dir)
    # Created before any handler is removed: a directory that cannot be created raises with the
    # process's logging still in place.
    if enable_file:
        log_path.mkdir(parents=True, exist_ok=True)

    # Define correlation-aware format
    if not log_format:
        log_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{extra[service]}</cyan> | "
            "<yellow>{extra[correlation_id]}</yellow> | "
            "<level>{message}</level>"
        )

    def correlation_filter(record: Record) -> bool:
        """Add the correlation ID and this service's name, each unless the call bound its own."""
        correlation_id = CorrelationContext.get()
        record["extra"].setdefault("correlation_id", correlation_id or "no-correlation")
        record["extra"].setdefault("service", ProcessService(service_name))
        return True

    sinks: list[tuple[Any, dict[str, Any]]] = [
        # Console handler with correlation ID
        (
            sys.stdout,
            {
                "format": log_format,
                "level": log_level,
                "filter": correlation_filter,
                "colorize": True,
                "enqueue": True,  # Async safety, as for every sink `LoggingConfig.configure` adds
            },
        )
    ]

    if enable_file:
        sinks.append(
            # File handler with correlation ID (text)
            (
                escape_braces(str(log_path / f"{log_file_stem(service_name)}.log")),
                {
                    "format": "{time} | {level} | {extra[service]} | {extra[correlation_id]} | {message}",
                    "level": log_level,
                    "filter": correlation_filter,
                    "rotation": "10 MB",
                    "retention": "7 days",
                    "compression": "zip",
                    "serialize": False,
                    "enqueue": True,
                },
            )
        )
        sinks.append(
            # Structured JSON logs for log aggregation systems
            (
                escape_braces(str(log_path / f"{log_file_stem(service_name)}.json")),
                {
                    "level": log_level,
                    "filter": correlation_filter,
                    "rotation": "10 MB",
                    "retention": "7 days",
                    "compression": "zip",
                    "serialize": True,  # JSON format
                    "enqueue": True,
                },
            )
        )

    # The sinks go in BEFORE anything is removed, as in `LoggingConfig.configure`: a sink that
    # fails to open (a directory that exists but cannot be written) must leave the process
    # logging as it was, and a failure takes out only what this call added.
    previous_ids = tuple(logger._core.handlers)  # type: ignore[attr-defined]
    added: list[int] = []
    try:
        for sink, options in sinks:
            added.append(logger.add(sink, **options))
    except BaseException:
        for handler_id in added:
            with suppress(ValueError):
                logger.remove(handler_id)
        raise
    if replace_existing:
        for previous in previous_ids:
            with suppress(ValueError):  # already removed by someone else since the snapshot
                logger.remove(previous)

    claim_process_service(
        service_name,
        replace_existing=replace_existing,
        what="Correlation-aware logging",
        set_when_unset=False,
    )

    logger.info(f"Correlation-aware logging configured for service: {service_name}")


def attach_correlation_id(record: Record) -> None:
    """Add the current correlation id to ``record``, unless the call bound one.

    A record written outside any request has no id and is left without the key.
    """
    correlation_id = CorrelationContext.get()
    if correlation_id is not None:
        record["extra"].setdefault("correlation_id", correlation_id)


def get_correlation_logger(name: str) -> Any:
    """
    Get a logger instance that attaches the current correlation ID to every record.

    The id is read when each line is written and added to the record itself, so
    it reaches every sink installed in the process, whoever added it.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Logger instance bound to ``module=name``
    """
    return logger.patch(attach_correlation_id).bind(module=name)

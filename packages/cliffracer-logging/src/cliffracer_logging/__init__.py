"""Logging that is not core's business: the NATS sink, correlation-filtered
sinks, the contextual logger, and per-dispatch timing as an extension.

`loguru` stays a core dependency because core logs; what moved here is
everything that CONFIGURES logging -- sinks, formats, files -- which a library
should not do to its host process without being asked.
"""

from .config import (
    ContextualLogger,
    LoggingConfig,
    LogRecordRedactor,
    get_service_logger,
    log_event_handling,
    log_rpc_calls,
    redact_sensitive_log_fields,
)
from .correlation_logging import (
    get_correlation_logger,
    setup_correlation_logging,
)
from .extension import LoggingExtension

__all__ = [
    "LoggingExtension",
    "LoggingConfig",
    "ContextualLogger",
    "LogRecordRedactor",
    "get_service_logger",
    "log_rpc_calls",
    "log_event_handling",
    "redact_sensitive_log_fields",
    "setup_correlation_logging",
    "get_correlation_logger",
]

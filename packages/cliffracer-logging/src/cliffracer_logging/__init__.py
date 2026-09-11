"""Logging that is not core's business: the NATS sink, correlation-filtered
sinks, the contextual logger, and per-dispatch timing as an extension.

`loguru` stays a core dependency because core logs; what moved here is
everything that CONFIGURES logging -- sinks, formats, files -- which a library
should not do to its host process without being asked.
"""

from .config import (
    ContextualLogger,
    LoggingConfig,
    get_service_logger,
    log_event_handling,
    log_rpc_calls,
)
from .correlation_logging import (
    CorrelationLoggerMixin,
    get_correlation_logger,
    setup_correlation_logging,
)
from .extension import LoggingExtension

__all__ = [
    "LoggingExtension",
    "LoggingConfig",
    "ContextualLogger",
    "get_service_logger",
    "log_rpc_calls",
    "log_event_handling",
    "setup_correlation_logging",
    "get_correlation_logger",
    "CorrelationLoggerMixin",
]

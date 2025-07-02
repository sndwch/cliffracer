"""
Enhanced logging with correlation ID support

This module provides logging configuration that automatically includes
correlation IDs in all log messages for better distributed tracing.
"""

import sys

from loguru import logger

from ..core.correlation import CorrelationContext


def setup_correlation_logging(
    service_name: str, log_level: str = "INFO", log_format: str | None = None
):
    """
    Configure loguru to include correlation IDs in all log messages.

    Args:
        service_name: Name of the service for log identification
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_format: Custom log format (uses sensible default if not provided)
    """
    # Remove default logger
    logger.remove()

    # Define correlation-aware format
    if not log_format:
        log_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{extra[service]}</cyan> | "
            "<yellow>{extra[correlation_id]}</yellow> | "
            "<level>{message}</level>"
        )

    def correlation_filter(record):
        """Add correlation ID and service name to log record"""
        correlation_id = CorrelationContext.get()
        record["extra"]["correlation_id"] = correlation_id or "no-correlation"
        record["extra"]["service"] = service_name
        return True

    # Add console handler with correlation ID
    logger.add(
        sys.stdout, format=log_format, level=log_level, filter=correlation_filter, colorize=True
    )

    # Add file handler with correlation ID (JSON format for structured logging)
    logger.add(
        f"logs/{service_name}.log",
        format="{time} | {level} | {extra[service]} | {extra[correlation_id]} | {message}",
        level=log_level,
        filter=correlation_filter,
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        serialize=False,  # Keep as text for now, can switch to JSON
    )

    # Add structured JSON logs for log aggregation systems
    logger.add(
        f"logs/{service_name}.json",
        level=log_level,
        filter=correlation_filter,
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        serialize=True,  # JSON format
    )

    logger.info(f"Correlation-aware logging configured for service: {service_name}")


def get_correlation_logger(name: str):
    """
    Get a logger instance that automatically includes correlation IDs.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Logger instance with correlation ID support
    """
    return logger.bind(module=name)


class CorrelationLoggerMixin:
    """
    Mixin to add correlation-aware logging to services.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Setup correlation logging for the service
        service_name = getattr(self.config, "name", "unknown_service")
        log_level = getattr(self.config, "log_level", "INFO")

        setup_correlation_logging(service_name, log_level)

        # Create service-specific logger
        self.logger = get_correlation_logger(self.__class__.__name__)

    def log_info(self, message: str, **kwargs):
        """Log info message with correlation ID"""
        self.logger.info(message, **kwargs)

    def log_error(self, message: str, **kwargs):
        """Log error message with correlation ID"""
        self.logger.error(message, **kwargs)

    def log_warning(self, message: str, **kwargs):
        """Log warning message with correlation ID"""
        self.logger.warning(message, **kwargs)

    def log_debug(self, message: str, **kwargs):
        """Log debug message with correlation ID"""
        self.logger.debug(message, **kwargs)

    def log_with_context(self, level: str, message: str, **context):
        """
        Log message with additional context and correlation ID.

        Args:
            level: Log level (info, error, warning, debug)
            message: Log message
            **context: Additional context to include
        """
        correlation_id = CorrelationContext.get()

        # Add correlation ID to context
        context["correlation_id"] = correlation_id

        # Log with appropriate level
        log_func = getattr(self.logger, level.lower(), self.logger.info)
        log_func(message, **context)

"""
Structured logging configuration using loguru
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from loguru import logger


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
    ):
        """
        Configure structured logging for a service

        Args:
            service_name: Name of the service for log identification
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            log_dir: Directory for log files (defaults to ./logs)
            structured: Whether to use structured JSON logging
            enable_console: Whether to log to console
            enable_file: Whether to log to file
            rotation: Log rotation policy
            retention: Log retention policy
            compression: Log compression format
        """
        # Remove default logger
        logger.remove()

        # Set log directory
        if log_dir is None:
            log_dir = os.getenv("LOG_DIR", "./logs")

        log_path = Path(log_dir)
        log_path.mkdir(exist_ok=True)

        # Configure formats
        if structured:
            # Structured JSON format for production
            json_format = (
                "{"
                '"timestamp": "{time:YYYY-MM-DD HH:mm:ss.SSS}", '
                '"level": "{level}", '
                '"service": "' + service_name + '", '
                '"module": "{module}", '
                '"function": "{function}", '
                '"line": {line}, '
                '"message": "{message}", '
                '"extra": {extra}'
                "}"
            )
            console_format = json_format
            file_format = json_format
        else:
            # Human-readable format for development
            console_format = (
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                f"<magenta>{service_name}</magenta> | "
                "<level>{message}</level>"
            )
            file_format = (
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
                "{level: <8} | "
                "{name}:{function}:{line} | "
                f"{service_name} | "
                "{message}"
            )

        # Console logging
        if enable_console:
            logger.add(
                sys.stderr,
                format=console_format,
                level=log_level,
                colorize=not structured,
                serialize=structured,
            )

        # File logging
        if enable_file:
            # Main log file
            logger.add(
                log_path / f"{service_name}.log",
                format=file_format,
                level=log_level,
                rotation=rotation,
                retention=retention,
                compression=compression,
                serialize=structured,
            )

            # Error-only log file
            logger.add(
                log_path / f"{service_name}_errors.log",
                format=file_format,
                level="ERROR",
                rotation=rotation,
                retention=retention,
                compression=compression,
                serialize=structured,
            )

        # Add service name to all log records
        logger.configure(extra={"service": service_name})

        logger.info(
            f"Logging configured for service '{service_name}'",
            log_level=log_level,
            structured=structured,
            log_dir=str(log_path),
        )


class ContextualLogger:
    """Logger with contextual information for microservices"""

    def __init__(self, service_name: str, context: dict[str, Any] | None = None):
        self.service_name = service_name
        self.context = context or {}
        self._logger = logger.bind(**self.context)

    def with_context(self, **kwargs) -> "ContextualLogger":
        """Create a new logger with additional context"""
        new_context = {**self.context, **kwargs}
        return ContextualLogger(self.service_name, new_context)

    def debug(self, message: str, **kwargs):
        """Log debug message with context"""
        self._logger.bind(**kwargs).debug(message)

    def info(self, message: str, **kwargs):
        """Log info message with context"""
        self._logger.bind(**kwargs).info(message)

    def warning(self, message: str, **kwargs):
        """Log warning message with context"""
        self._logger.bind(**kwargs).warning(message)

    def error(self, message: str, **kwargs):
        """Log error message with context"""
        self._logger.bind(**kwargs).error(message)

    def critical(self, message: str, **kwargs):
        """Log critical message with context"""
        self._logger.bind(**kwargs).critical(message)

    def exception(self, message: str, **kwargs):
        """Log exception with traceback and context"""
        self._logger.bind(**kwargs).exception(message)


def get_service_logger(service_name: str, **context) -> ContextualLogger:
    """Get a contextual logger for a service"""
    return ContextualLogger(service_name, context)


# Decorator for automatic request/response logging
def log_rpc_calls(logger_instance: ContextualLogger):
    """Decorator to automatically log RPC calls"""

    def decorator(func):
        async def wrapper(*args, **kwargs):
            # Extract service instance (usually first arg)
            service = args[0] if args else None
            service_name = (
                getattr(service, "config", {}).get("name", "unknown") if service else "unknown"
            )

            # Create request context
            request_logger = logger_instance.with_context(
                rpc_method=func.__name__,
                service=service_name,
                request_args=str(kwargs) if kwargs else "no_args",
            )

            request_logger.info(f"RPC call started: {func.__name__}")

            try:
                # Execute the function
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)

                request_logger.info(
                    f"RPC call completed: {func.__name__}", result_type=type(result).__name__
                )
                return result

            except Exception as e:
                request_logger.error(
                    f"RPC call failed: {func.__name__}", error=str(e), error_type=type(e).__name__
                )
                raise

        return wrapper

    return decorator


def log_event_handling(logger_instance: ContextualLogger):
    """Decorator to automatically log event handling"""

    def decorator(func):
        async def wrapper(*args, **kwargs):
            service = args[0] if args else None
            service_name = (
                getattr(service, "config", {}).get("name", "unknown") if service else "unknown"
            )

            event_logger = logger_instance.with_context(
                event_handler=func.__name__,
                service=service_name,
                event_data=str(kwargs) if kwargs else "no_data",
            )

            event_logger.debug(f"Event handling started: {func.__name__}")

            try:
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)

                event_logger.debug(f"Event handling completed: {func.__name__}")
                return result

            except Exception as e:
                event_logger.error(
                    f"Event handling failed: {func.__name__}",
                    error=str(e),
                    error_type=type(e).__name__,
                )
                raise

        return wrapper

    return decorator


# asyncio is already imported at the top of the file

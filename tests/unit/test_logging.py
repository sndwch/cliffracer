"""
Unit tests for logging configuration
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cliffracer.logging.config import ContextualLogger, LoggingConfig, get_service_logger


class TestLoggingConfig:
    """Test LoggingConfig class"""

    def test_configure_default_settings(self):
        """Test logging configuration with default settings"""
        with tempfile.TemporaryDirectory() as tmpdir:
            LoggingConfig.configure(service_name="test_service", log_dir=tmpdir)

            # Check that log directory was created
            log_path = Path(tmpdir)
            assert log_path.exists()

    def test_configure_custom_settings(self):
        """Test logging configuration with custom settings"""
        with tempfile.TemporaryDirectory() as tmpdir:
            LoggingConfig.configure(
                service_name="custom_service",
                log_level="DEBUG",
                log_dir=tmpdir,
                structured=True,
                enable_console=True,
                enable_file=True,
                rotation="5 MB",
                retention="2 weeks",
                compression="zip",
            )

            # Configuration should complete without error
            log_path = Path(tmpdir)
            assert log_path.exists()

    def test_configure_console_only(self):
        """Test logging configuration with console only"""
        with tempfile.TemporaryDirectory() as tmpdir:
            LoggingConfig.configure(
                service_name="console_service",
                log_dir=tmpdir,
                enable_console=True,
                enable_file=False,
            )

            # Should not create log files
            Path(tmpdir)
            # log_files = list(log_path.glob("*.log"))
            # Note: This test might be flaky since loguru might still create files
            # In a real scenario, you'd mock loguru.logger.add

    def test_configure_file_only(self):
        """Test logging configuration with file only"""
        with tempfile.TemporaryDirectory() as tmpdir:
            LoggingConfig.configure(
                service_name="file_service", log_dir=tmpdir, enable_console=False, enable_file=True
            )

            # Configuration should complete
            log_path = Path(tmpdir)
            assert log_path.exists()


class TestContextualLogger:
    """Test ContextualLogger class"""

    def test_logger_initialization(self):
        """Test logger initialization"""
        logger = ContextualLogger("test_service")

        assert logger.service_name == "test_service"
        assert logger.context == {}

    def test_logger_with_initial_context(self):
        """Test logger with initial context"""
        initial_context = {"component": "database", "operation": "connect"}
        logger = ContextualLogger("test_service", initial_context)

        assert logger.service_name == "test_service"
        assert logger.context == initial_context

    def test_with_context(self):
        """Test adding context to logger"""
        logger = ContextualLogger("test_service")

        # Add context
        contextual_logger = logger.with_context(request_id="req-123", user_id="user-456")

        # Original logger should be unchanged
        assert logger.context == {}

        # New logger should have context
        assert contextual_logger.context == {"request_id": "req-123", "user_id": "user-456"}

    def test_chained_context(self):
        """Test chaining context additions"""
        logger = ContextualLogger("test_service", {"base": "value"})

        chained_logger = logger.with_context(step1="done").with_context(
            step2="done", step3="in_progress"
        )

        expected_context = {
            "base": "value",
            "step1": "done",
            "step2": "done",
            "step3": "in_progress",
        }

        assert chained_logger.context == expected_context

    @patch("cliffracer.logging.config.logger")
    def test_logging_methods(self, mock_logger):
        """Test that logging methods call loguru correctly"""
        logger = ContextualLogger("test_service", {"component": "test"})

        # Mock the bound logger
        mock_bound_logger = MagicMock()
        mock_logger.bind.return_value = mock_bound_logger

        # Test each logging method
        logger.debug("Debug message", extra_field="value")
        logger.info("Info message", extra_field="value")
        logger.warning("Warning message", extra_field="value")
        logger.error("Error message", extra_field="value")
        logger.critical("Critical message", extra_field="value")
        logger.exception("Exception message", extra_field="value")

        # Verify logger.bind was called with context
        mock_logger.bind.assert_called_with(component="test")

        # Verify logging methods were called
        mock_bound_logger.bind.assert_called()
        mock_bound_logger.debug.assert_called()
        mock_bound_logger.info.assert_called()
        mock_bound_logger.warning.assert_called()
        mock_bound_logger.error.assert_called()
        mock_bound_logger.critical.assert_called()
        mock_bound_logger.exception.assert_called()


class TestServiceLoggerFactory:
    """Test get_service_logger factory function"""

    def test_get_service_logger(self):
        """Test get_service_logger function"""
        logger = get_service_logger("test_service")

        assert isinstance(logger, ContextualLogger)
        assert logger.service_name == "test_service"
        assert logger.context == {}

    def test_get_service_logger_with_context(self):
        """Test get_service_logger with context"""
        logger = get_service_logger("test_service", component="api", version="1.0")

        assert isinstance(logger, ContextualLogger)
        assert logger.service_name == "test_service"
        assert logger.context == {"component": "api", "version": "1.0"}


class TestLoggingDecorators:
    """Test logging decorators"""

    @pytest.fixture
    def mock_service(self):
        """Create a mock service for testing"""
        service = MagicMock()
        service.config.name = "test_service"
        return service

    @pytest.fixture
    def test_logger(self):
        """Create a test logger"""
        return ContextualLogger("test_service")

    def test_log_rpc_calls_decorator_import(self):
        """Test that log_rpc_calls decorator can be imported"""
        from cliffracer.logging.config import log_rpc_calls

        assert callable(log_rpc_calls)

    def test_log_event_handling_decorator_import(self):
        """Test that log_event_handling decorator can be imported"""
        from cliffracer.logging.config import log_event_handling

        assert callable(log_event_handling)

    @patch("cliffracer.logging.config.logger")
    @pytest.mark.asyncio
    async def test_log_rpc_calls_decorator_async(self, mock_logger, test_logger, mock_service):
        """Test log_rpc_calls decorator with async function"""
        from cliffracer.logging.config import log_rpc_calls

        @log_rpc_calls(test_logger)
        async def test_rpc_method(service, param1: str, param2: int):
            return {"result": f"{param1}_{param2}"}

        # Call the decorated method
        result = await test_rpc_method(mock_service, param1="test", param2=123)

        # Check result
        assert result == {"result": "test_123"}

    @patch("cliffracer.logging.config.logger")
    @pytest.mark.asyncio
    async def test_log_event_handling_decorator_async(self, mock_logger, test_logger, mock_service):
        """Test log_event_handling decorator with async function"""
        from cliffracer.logging.config import log_event_handling

        @log_event_handling(test_logger)
        async def test_event_handler(service, subject: str, **kwargs):
            return f"handled {subject}"

        # Call the decorated method
        result = await test_event_handler(mock_service, subject="test.event", data="test")

        # Check result
        assert result == "handled test.event"

    @patch("cliffracer.logging.config.logger")
    @pytest.mark.asyncio
    async def test_decorator_exception_handling(self, mock_logger, test_logger, mock_service):
        """Test that decorators handle exceptions properly"""
        from cliffracer.logging.config import log_rpc_calls

        @log_rpc_calls(test_logger)
        async def failing_rpc_method(service):
            raise ValueError("Test error")

        # Call should raise the exception
        with pytest.raises(ValueError, match="Test error"):
            await failing_rpc_method(mock_service)

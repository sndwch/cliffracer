"""
Unit tests for logging configuration
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cliffracer_logging.config import ContextualLogger, LoggingConfig, get_service_logger
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from conftest import broker_url

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_test_leaves_a_sink_it_added():
    """Fail a test that finishes with a sink it added still attached to the logger.

    `LoggingConfig.configure` attaches sinks pointed at a directory the test then deletes. A sink
    left attached keeps writing there and is present for every test that runs after, so what a later
    test sees depends on which ran before it. Only a sink that is new at the end of the test is
    reported: `configure` begins with a bare `logger.remove()`, which also takes the sinks that were
    there before.
    """
    before = set(logger._core.handlers)
    yield
    leaked = sorted(set(logger._core.handlers) - before)
    for handler_id in leaked:  # so one leak is one failure, not every test after it
        logger.remove(handler_id)
    assert not leaked, f"the test left {len(leaked)} sink(s) it added attached to the logger"


class TestLoggingConfig:
    """Test LoggingConfig class"""

    @pytest.fixture(autouse=True)
    def _detach_what_configure_attached(self):
        """`configure` attaches sinks pointed at the temporary directory the test then deletes."""
        before = set(logger._core.handlers)
        yield
        for handler_id in set(logger._core.handlers) - before:
            logger.remove(handler_id)

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
            log_files = list(Path(tmpdir).glob("*.log"))
            assert len(log_files) == 0

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

    def test_logging_methods(self):
        """Test that logging methods work correctly"""
        # Create a logger with test context
        test_logger = ContextualLogger("test_service", {"component": "test"})

        # Mock the internal _logger
        mock_logger = MagicMock()
        mock_bound_logger = MagicMock()
        mock_located_logger = MagicMock()
        mock_logger.bind.return_value = mock_bound_logger
        mock_bound_logger.opt.return_value = mock_located_logger
        test_logger._logger = mock_logger

        # Test each logging method
        test_logger.debug("Debug message", extra_field="value")
        test_logger.info("Info message", extra_field="value")
        test_logger.warning("Warning message", extra_field="value")
        test_logger.error("Error message", extra_field="value")
        test_logger.critical("Critical message", extra_field="value")
        test_logger.exception("Exception message", extra_field="value")

        # Verify bind was called for each log method with extra fields
        assert mock_logger.bind.call_count == 6  # One for each log method
        mock_logger.bind.assert_called_with(extra_field="value")
        # Each line is written one frame up, so it reports the caller's location
        assert mock_bound_logger.opt.call_count == 6
        mock_bound_logger.opt.assert_called_with(depth=1)

        # Verify logging methods were called
        mock_located_logger.debug.assert_called_once_with("Debug message")
        mock_located_logger.info.assert_called_once_with("Info message")
        mock_located_logger.warning.assert_called_once_with("Warning message")
        mock_located_logger.error.assert_called_once_with("Error message")
        mock_located_logger.critical.assert_called_once_with("Critical message")
        mock_located_logger.exception.assert_called_once_with("Exception message")


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
        return CliffracerService(ServiceConfig(name="test_service", nats_url=broker_url()))

    @pytest.fixture
    def test_logger(self):
        """Create a test logger"""
        return ContextualLogger("test_service")

    @patch("cliffracer_logging.config.logger")
    @pytest.mark.asyncio
    async def test_log_rpc_calls_decorator_async(self, mock_logger, test_logger, mock_service):
        """Test log_rpc_calls decorator with async function"""
        from cliffracer_logging.config import log_rpc_calls

        @log_rpc_calls(test_logger)
        async def test_rpc_method(service, param1: str, param2: int):
            return {"result": f"{param1}_{param2}"}

        # Call the decorated method
        result = await test_rpc_method(mock_service, param1="test", param2=123)

        # Check result
        assert result == {"result": "test_123"}

    @patch("cliffracer_logging.config.logger")
    @pytest.mark.asyncio
    async def test_log_event_handling_decorator_async(self, mock_logger, test_logger, mock_service):
        """Test log_event_handling decorator with async function"""
        from cliffracer_logging.config import log_event_handling

        @log_event_handling(test_logger)
        async def test_event_handler(service, subject: str, **kwargs):
            return f"handled {subject}"

        # Call the decorated method
        result = await test_event_handler(mock_service, subject="test.event", data="test")

        # Check result
        assert result == "handled test.event"

    @patch("cliffracer_logging.config.logger")
    @pytest.mark.asyncio
    async def test_decorator_exception_handling(self, mock_logger, test_logger, mock_service):
        """Test that decorators handle exceptions properly"""
        from cliffracer_logging.config import log_rpc_calls

        @log_rpc_calls(test_logger)
        async def failing_rpc_method(service):
            raise ValueError("Test error")

        # Call should raise the exception
        with pytest.raises(ValueError, match="Test error"):
            await failing_rpc_method(mock_service)

    @patch("cliffracer_logging.config.logger")
    @pytest.mark.asyncio
    async def test_decorators_with_real_service_config(self, mock_logger, test_logger):
        """Verify logging decorators handle ServiceConfig BaseModel instances."""
        from cliffracer_logging.config import log_event_handling, log_rpc_calls

        from cliffracer import CliffracerService, ServiceConfig

        svc = CliffracerService(ServiceConfig(name="real_service_name"))

        @log_rpc_calls(test_logger)
        async def my_rpc(service):
            return "ok"

        @log_event_handling(test_logger)
        async def my_event(service, subject="test.event"):
            return "event_ok"

        res_rpc = await my_rpc(svc)
        assert res_rpc == "ok"
        res_evt = await my_event(svc)
        assert res_evt == "event_ok"

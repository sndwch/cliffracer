"""Unit tests for setup_correlation_logging and correlation ID log output."""

import pytest

from cliffracer import CorrelationContext, set_correlation_id


@pytest.mark.asyncio
async def test_correlation_logging():
    """Test that correlation IDs appear in logs"""
    import os
    import tempfile

    from cliffracer_logging import setup_correlation_logging
    from loguru import logger

    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup logging with temp directory
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        os.makedirs("logs", exist_ok=True)

        try:
            setup_correlation_logging("test_service", "DEBUG")

            # Set correlation ID
            set_correlation_id("log_test_789")

            # Log a message
            logger.info("Test log message")

            # Read log file
            with open("logs/test_service.log") as f:
                log_content = f.read()

            # Verify correlation ID is in log
            assert "log_test_789" in log_content
            assert "Test log message" in log_content

        finally:
            os.chdir(old_cwd)
            CorrelationContext.clear()

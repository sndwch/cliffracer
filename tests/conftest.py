"""
Pytest configuration and fixtures for Cliffracer testing
"""

import asyncio
import os
import tempfile
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from cliffracer import ServiceConfig, ValidatedNATSService, LoggedExtendedService


# Configure asyncio for pytest
@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def temp_log_dir():
    """Create a temporary directory for test logs"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def test_config() -> ServiceConfig:
    """Create a test service configuration"""
    return ServiceConfig(
        name="test_service",
        nats_url="nats://localhost:4222",
        auto_restart=False,  # Don't restart during tests
        request_timeout=5.0,
    )


@pytest.fixture
def mock_nats_client():
    """Create a mock NATS client"""
    mock_client = AsyncMock()
    mock_client.is_closed = False
    mock_client.request = AsyncMock()
    mock_client.publish = AsyncMock()
    mock_client.subscribe = AsyncMock()
    mock_client.drain = AsyncMock()
    mock_client.close = AsyncMock()
    return mock_client


@pytest.fixture
def mock_service_config():
    """Create a mock service configuration"""
    config = MagicMock(spec=ServiceConfig)
    config.name = "test_service"
    config.nats_url = "nats://localhost:4222"
    config.auto_restart = False
    config.request_timeout = 5.0
    config.max_reconnect_attempts = 3
    config.reconnect_time_wait = 1
    config.jetstream_enabled = False
    return config


@pytest_asyncio.fixture
async def test_service(test_config) -> AsyncGenerator[ValidatedNATSService]:
    """Create a test service instance"""
    service = ValidatedNATSService(test_config)

    # Mock the NATS connection
    service.nc = AsyncMock()
    service.nc.is_closed = False
    service._running = True

    yield service

    # Cleanup
    if hasattr(service, "stop"):
        try:
            await service.stop()
        except:
            pass


@pytest_asyncio.fixture
async def logged_test_service(
    test_config, temp_log_dir
) -> AsyncGenerator[LoggedExtendedService]:
    """Create a logged test service instance"""
    # Set log directory for test
    os.environ["LOG_DIR"] = temp_log_dir
    os.environ["LOG_LEVEL"] = "DEBUG"

    service = LoggedExtendedService(test_config)

    # Mock the NATS connection
    service.nc = AsyncMock()
    service.nc.is_closed = False
    service._running = True

    yield service

    # Cleanup
    if hasattr(service, "stop"):
        try:
            await service.stop()
        except:
            pass


@pytest.fixture
def sample_rpc_request():
    """Sample RPC request data"""
    return {"username": "test_user", "email": "test@example.com", "full_name": "Test User"}


@pytest.fixture
def sample_event_data():
    """Sample event data"""
    return {"user_id": "user_123", "action": "login", "timestamp": "2023-01-01T00:00:00Z"}


@pytest.fixture
def sample_broadcast_message():
    """Sample broadcast message"""
    return {
        "user_id": "user_123",
        "username": "test_user",
        "email": "test@example.com",
        "source_service": "user_service",
    }


# Test utilities
class MockMessage:
    """Mock NATS message for testing"""

    def __init__(self, subject: str, data: bytes, reply: str = None):
        self.subject = subject
        self.data = data
        self.reply = reply
        self._response_sent = False

    async def respond(self, data: bytes):
        """Mock respond method"""
        self._response_sent = True
        self.response_data = data


class TestServiceHelper:
    """Helper class for service testing"""

    @staticmethod
    def create_mock_message(subject: str, data: dict = None, reply: str = None) -> MockMessage:
        """Create a mock NATS message"""
        import json

        message_data = json.dumps(data or {}).encode()
        return MockMessage(subject, message_data, reply)

    @staticmethod
    async def wait_for_condition(condition_func, timeout: float = 5.0, interval: float = 0.1):
        """Wait for a condition to become true"""
        import time

        start_time = time.time()

        while time.time() - start_time < timeout:
            if condition_func():
                return True
            await asyncio.sleep(interval)

        return False


@pytest.fixture
def test_helper():
    """Test helper utilities"""
    return TestServiceHelper


# Skip integration tests if NATS is not available
def pytest_configure(config):
    """Configure pytest with custom markers"""
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line("markers", "unit: mark test as unit test")
    config.addinivalue_line("markers", "nats_required: mark test as requiring NATS server")


def pytest_collection_modifyitems(config, items):
    """Modify test collection to handle markers"""
    # Skip NATS integration tests if NATS_SKIP_INTEGRATION is set
    if os.getenv("NATS_SKIP_INTEGRATION", "false").lower() == "true":
        skip_integration = pytest.mark.skip(reason="NATS integration tests disabled")
        for item in items:
            if "nats_required" in item.keywords:
                item.add_marker(skip_integration)

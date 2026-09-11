"""
Pytest configuration and fixtures for Cliffracer testing
"""

import asyncio
import tempfile
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import nats
import pytest
import pytest_asyncio

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.correlation import correlation_id_var


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
        nats_url=broker_url(),
        auto_restart=False,  # Don't restart during tests
        request_timeout=5.0,
    )


@pytest_asyncio.fixture
async def nats_connection():
    """Connect to a real NATS server for integration tests.

    Yields a live nats.aio client connected to the suite's broker and
    closes it on teardown. Tests using this fixture should be marked
    @pytest.mark.nats_required.
    """
    import nats

    nc = await nats.connect(broker_url())
    try:
        yield nc
    finally:
        if not nc.is_closed:
            await nc.drain()


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
    config.nats_url = broker_url()
    config.auto_restart = False
    config.request_timeout = 5.0
    config.max_reconnect_attempts = 3
    config.reconnect_time_wait = 1
    config.jetstream_enabled = False
    return config


@pytest_asyncio.fixture
async def test_service(test_config) -> AsyncGenerator[CliffracerService]:
    """Create a test service instance"""
    service = CliffracerService(test_config)

    # Mock the NATS connection completely
    service.nc = AsyncMock()
    service.nc.is_closed = False
    service._running = True

    # Mock connection methods to prevent actual NATS calls
    service.start = AsyncMock()
    service.stop = AsyncMock()
    service._connect = AsyncMock()
    service._setup_subscriptions = AsyncMock()

    yield service


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

    def __init__(self, subject: str, data: bytes, reply: str = "_INBOX.test"):
        self.subject = subject
        self.data = data
        self.reply = reply
        self._response_sent = False

    async def respond(self, data: bytes):
        """Mock respond method"""
        if not self.reply:
            raise nats.errors.Error("no reply subject available")
        self._response_sent = True
        self.response_data = data


class TestServiceHelper:
    """Helper class for service testing"""

    @staticmethod
    def create_mock_message(
        subject: str, data: dict = None, reply: str = "_INBOX.test"
    ) -> MockMessage:
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


# --- Broker machinery re-exports -------------------------------------------
from conftest import (  # noqa: E402
    DEFAULT_BROKER_URL,
    NATS_PROBE_TIMEOUT_S,
    TEST_BROKER_URL_ENV,
    _apply_broker_url,
    _broker_is_listening,
    broker_url,
    configured_broker_url,
)

__all__ = [
    "DEFAULT_BROKER_URL",
    "NATS_PROBE_TIMEOUT_S",
    "TEST_BROKER_URL_ENV",
    "_apply_broker_url",
    "_broker_is_listening",
    "broker_url",
    "configured_broker_url",
]


def declared(svc):
    """Return user-declared extension names, excluding container built-ins."""
    return [e.name for e in svc._extensions if not e.name.startswith("_")]


@pytest.fixture(autouse=True)
def _reset_correlation_id_var():
    """Reset correlation_id_var after every test to prevent context leaks."""
    yield
    correlation_id_var.set(None)

"""
Unit tests for ServiceConfig
"""

import pytest

from cliffracer import ServiceConfig
from tests.conftest import broker_url

pytestmark = pytest.mark.unit


class TestServiceConfig:
    """Test ServiceConfig class"""

    def test_default_config(self):
        """Test ServiceConfig with default values"""
        config = ServiceConfig(name="test_service")

        assert config.name == "test_service"
        # broker_url() reflects $CLIFFRACER_TEST_NATS_URL if set.
        assert config.nats_url == broker_url()
        # -1 enables infinite reconnect attempts in nats-py.
        assert config.max_reconnect_attempts == -1
        assert config.reconnect_time_wait == 2
        assert config.exit_on_closed is True
        assert config.version == "0.1.0"

    def test_custom_config(self):
        """Test ServiceConfig with custom values"""
        config = ServiceConfig(
            name="custom_service",
            nats_url="nats://remote:4222",
            max_reconnect_attempts=10,
            reconnect_time_wait=5,
            version="1.0.0",
        )

        assert config.name == "custom_service"
        assert config.nats_url == "nats://remote:4222"
        assert config.max_reconnect_attempts == 10
        assert config.reconnect_time_wait == 5
        assert config.version == "1.0.0"

    def test_config_mutability(self):
        """Test that config can be modified after creation"""
        config = ServiceConfig(name="test_service")

        # Pydantic models are mutable by default
        config.name = "new_name"
        assert config.name == "new_name"

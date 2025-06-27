"""
Unit tests for ServiceConfig
"""

import pytest

from nats_service import ServiceConfig


class TestServiceConfig:
    """Test ServiceConfig class"""

    def test_default_config(self):
        """Test ServiceConfig with default values"""
        config = ServiceConfig(name="test_service")

        assert config.name == "test_service"
        assert config.nats_url == "nats://localhost:4222"
        assert config.max_reconnect_attempts == 60
        assert config.reconnect_time_wait == 2
        assert config.request_timeout == 30.0
        assert config.auto_restart is True
        assert config.jetstream_enabled is False

    def test_custom_config(self):
        """Test ServiceConfig with custom values"""
        config = ServiceConfig(
            name="custom_service",
            nats_url="nats://remote:4222",
            max_reconnect_attempts=10,
            reconnect_time_wait=5,
            request_timeout=60.0,
            auto_restart=False,
            jetstream_enabled=True,
        )

        assert config.name == "custom_service"
        assert config.nats_url == "nats://remote:4222"
        assert config.max_reconnect_attempts == 10
        assert config.reconnect_time_wait == 5
        assert config.request_timeout == 60.0
        assert config.auto_restart is False
        assert config.jetstream_enabled is True

    def test_config_immutability(self):
        """Test that config is immutable after creation"""
        config = ServiceConfig(name="test_service")

        # dataclass should be frozen, so this should raise an error
        with pytest.raises(AttributeError):
            config.name = "new_name"

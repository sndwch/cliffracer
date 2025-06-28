"""
Unit tests for ServiceConfig
"""


from cliffracer import ServiceConfig


class TestServiceConfig:
    """Test ServiceConfig class"""

    def test_default_config(self):
        """Test ServiceConfig with default values"""
        config = ServiceConfig(name="test_service")

        assert config.name == "test_service"
        assert config.nats_url == "nats://localhost:4222"
        assert config.max_reconnect_attempts == 60
        assert config.reconnect_time_wait == 2
        assert config.health_check_interval == 30
        assert config.health_check_timeout == 5
        assert config.version == "0.1.0"
        assert config.backdoor_enabled is True
        assert config.disable_backdoor is False

    def test_custom_config(self):
        """Test ServiceConfig with custom values"""
        config = ServiceConfig(
            name="custom_service",
            nats_url="nats://remote:4222",
            max_reconnect_attempts=10,
            reconnect_time_wait=5,
            health_check_interval=60,
            version="1.0.0",
            backdoor_enabled=False,
        )

        assert config.name == "custom_service"
        assert config.nats_url == "nats://remote:4222"
        assert config.max_reconnect_attempts == 10
        assert config.reconnect_time_wait == 5
        assert config.health_check_interval == 60
        assert config.version == "1.0.0"
        assert config.backdoor_enabled is False

    def test_config_mutability(self):
        """Test that config can be modified after creation"""
        config = ServiceConfig(name="test_service")

        # Pydantic models are mutable by default
        config.name = "new_name"
        assert config.name == "new_name"

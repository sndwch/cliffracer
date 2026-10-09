"""
Unit tests for ServiceConfig
"""

import pytest

from cliffracer import ServiceConfig
from tests.conftest import DEFAULT_BROKER_URL, configured_broker_url

pytestmark = pytest.mark.unit


class TestServiceConfig:
    """Test ServiceConfig class"""

    def test_default_config(self):
        """Test ServiceConfig with default values"""
        config = ServiceConfig(name="test_service")

        assert config.name == "test_service"
        # The suite's one address: what the operator pointed it at, else the default the suite
        # captured before any override. The literal default itself is pinned once, in
        # tests/repo/test_the_suite_has_one_broker_url.py::test_the_default_is_unchanged_when_nobody_asks,
        # which is the only place a test may name an address.
        assert config.nats_url == (configured_broker_url() or DEFAULT_BROKER_URL)
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

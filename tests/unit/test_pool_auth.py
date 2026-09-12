"""Tests for ServiceConfig.nats_auth_kwargs authentication mapping."""

import pytest

from cliffracer import ServiceConfig

pytestmark = pytest.mark.unit


class TestServiceConfigAuthKwargs:
    """Tests for ServiceConfig authentication keyword argument generation."""

    def test_no_credentials_yields_an_empty_mapping(self):
        assert ServiceConfig(name="s").nats_auth_kwargs() == {}

    def test_only_configured_credentials_appear(self):
        cfg = ServiceConfig(name="s", nats_user="u", nats_password="p")
        assert cfg.nats_auth_kwargs() == {"user": "u", "password": "p"}

    def test_token_and_credentials_file_map_to_nats_py_names(self):
        cfg = ServiceConfig(name="s", nats_token="t", nats_credentials_file="/c.creds")
        assert cfg.nats_auth_kwargs() == {"token": "t", "user_credentials": "/c.creds"}

    def test_absent_keys_are_omitted_not_none(self):
        """Ensure unconfigured authentication keys are excluded from the mapping."""
        keys = ServiceConfig(name="s", nats_user="u").nats_auth_kwargs()
        assert set(keys) == {"user"}

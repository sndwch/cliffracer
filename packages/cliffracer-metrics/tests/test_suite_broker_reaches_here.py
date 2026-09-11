"""Verify test suite broker configuration reaches package tests."""

import os

import pytest

from cliffracer import ServiceConfig

BROKER_ENV = "CLIFFRACER_TEST_NATS_URL"

pytestmark = pytest.mark.unit


def test_a_service_built_under_packages_dials_the_configured_broker():
    asked = os.getenv(BROKER_ENV)
    if not asked:
        pytest.skip(f"${BROKER_ENV} is not set; nothing to check against")

    assert ServiceConfig(name="reach-probe").nats_url == asked


def test_CONTROL_an_explicit_url_still_wins():
    """Verify explicit nats_url overrides environment configuration."""
    named = ServiceConfig(name="reach-probe", nats_url="nats://elsewhere:4222")

    assert named.nats_url == "nats://elsewhere:4222"
    assert ServiceConfig(name="reach-probe").nats_url != "nats://elsewhere:4222"

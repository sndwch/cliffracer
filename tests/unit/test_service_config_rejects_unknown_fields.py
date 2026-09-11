"""Tests verifying ServiceConfig forbids unknown or deprecated configuration fields."""

import pytest
from pydantic import ValidationError

from cliffracer import ServiceConfig

# Deprecated fields that must be refused by name.
REMOVED = [
    "queue_group",
    "max_restart_attempts",
    "log_structured",
    "log_to_file",
    "log_to_console",
    "health_check_interval",
    "health_check_timeout",
]


@pytest.mark.unit
@pytest.mark.parametrize("field", REMOVED)
def test_a_removed_field_is_refused_by_name(field):
    with pytest.raises(ValidationError) as exc:
        ServiceConfig(name="x", **{field: True})
    assert field in str(exc.value), str(exc.value)


@pytest.mark.unit
def test_a_misspelled_field_is_refused_too():
    with pytest.raises(ValidationError) as exc:
        ServiceConfig(name="x", nats_ul="nats://localhost:4222")
    assert "nats_ul" in str(exc.value)


@pytest.mark.unit
def test_CONTROL_a_real_field_is_still_accepted():
    """Verify valid configuration fields are accepted without errors."""
    config = ServiceConfig(name="x", nats_url="nats://example:4222", auto_restart=False)
    assert config.nats_url == "nats://example:4222"
    assert config.auto_restart is False

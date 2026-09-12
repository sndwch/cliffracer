"""Strict bounds and assignment validation tests for ServiceConfig."""

import pytest
from pydantic import ValidationError

from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit


def test_concurrency_bounds_reject_zero_and_negative() -> None:
    """Verify max_rpc_concurrency, max_event_concurrency, and max_async_rpc_concurrency require gt=0."""
    with pytest.raises(ValidationError):
        ServiceConfig(name="test", max_rpc_concurrency=0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", max_rpc_concurrency=-1)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", max_event_concurrency=0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", max_event_concurrency=-5)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", max_async_rpc_concurrency=0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", max_async_rpc_concurrency=-10)

    # Positive and None values are valid
    cfg1 = ServiceConfig(name="test", max_rpc_concurrency=1)
    assert cfg1.max_rpc_concurrency == 1

    cfg2 = ServiceConfig(name="test", max_rpc_concurrency=None)
    assert cfg2.max_rpc_concurrency is None


def test_timeout_bounds_reject_zero_and_negative() -> None:
    """Verify connect_timeout, shutdown_timeout, and request_timeout require gt=0."""
    with pytest.raises(ValidationError):
        ServiceConfig(name="test", connect_timeout=0.0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", connect_timeout=-1.0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", shutdown_timeout=0.0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", shutdown_timeout=-0.5)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", request_timeout=0.0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", request_timeout=-5.0)

    cfg = ServiceConfig(name="test", connect_timeout=0.1, shutdown_timeout=1.0, request_timeout=5.0)
    assert cfg.connect_timeout == 0.1
    assert cfg.shutdown_timeout == 1.0
    assert cfg.request_timeout == 5.0


def test_reconnect_and_restart_bounds() -> None:
    """Verify max_reconnect_attempts ge=-1, reconnect_time_wait ge=0, and restart_delay ge=0."""
    # -1 is valid (infinite reconnect)
    cfg = ServiceConfig(name="test", max_reconnect_attempts=-1)
    assert cfg.max_reconnect_attempts == -1

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", max_reconnect_attempts=-2)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", reconnect_time_wait=-1)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", restart_delay=-0.1)

    cfg_valid = ServiceConfig(name="test", reconnect_time_wait=0, restart_delay=0.0)
    assert cfg_valid.reconnect_time_wait == 0
    assert cfg_valid.restart_delay == 0.0


def test_jetstream_tuning_bounds() -> None:
    """Verify JetStream delivery, batch, timeout, and backoff bounds."""
    with pytest.raises(ValidationError):
        ServiceConfig(name="test", jetstream_max_deliver=0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", jetstream_max_deliver=-1)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", jetstream_ack_wait=0.0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", jetstream_ack_wait=-1.0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", jetstream_max_ack_pending=0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", jetstream_pull_batch=0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", jetstream_pull_timeout=0.0)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", jetstream_nak_backoff=-0.5)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", jetstream_max_backoff=-1.0)

    cfg = ServiceConfig(
        name="test",
        jetstream_max_deliver=1,
        jetstream_ack_wait=0.5,
        jetstream_max_ack_pending=1,
        jetstream_pull_batch=1,
        jetstream_pull_timeout=1.0,
        jetstream_nak_backoff=0.0,
        jetstream_max_backoff=0.0,
    )
    assert cfg.jetstream_max_deliver == 1


def test_health_port_bounds() -> None:
    """Verify health_port must be in range [0, 65535]."""
    with pytest.raises(ValidationError):
        ServiceConfig(name="test", health_port=-1)

    with pytest.raises(ValidationError):
        ServiceConfig(name="test", health_port=65536)

    cfg0 = ServiceConfig(name="test", health_port=0)
    assert cfg0.health_port == 0

    cfg_max = ServiceConfig(name="test", health_port=65535)
    assert cfg_max.health_port == 65535


def test_validate_assignment_rejects_invalid_mutations() -> None:
    """Verify runtime attribute mutations trigger ValidationError immediately."""
    cfg = ServiceConfig(name="valid_service", max_rpc_concurrency=10)

    with pytest.raises(ValidationError):
        cfg.max_rpc_concurrency = 0

    with pytest.raises(ValidationError):
        cfg.max_rpc_concurrency = -1

    with pytest.raises(ValidationError):
        cfg.max_event_concurrency = 0

    with pytest.raises(ValidationError):
        cfg.max_async_rpc_concurrency = 0

    with pytest.raises(ValidationError):
        cfg.connect_timeout = 0.0

    with pytest.raises(ValidationError):
        cfg.shutdown_timeout = -1.0

    with pytest.raises(ValidationError):
        cfg.request_timeout = 0.0

    with pytest.raises(ValidationError):
        cfg.health_port = -1

    with pytest.raises(ValidationError):
        cfg.health_port = 70000

    with pytest.raises(ValidationError):
        cfg.name = ""

    with pytest.raises(ValidationError):
        cfg.name = "has whitespace"

    with pytest.raises(ValidationError):
        cfg.name = "wildcard*name"

    with pytest.raises(ValidationError):
        cfg.namespace = "invalid.token"

    # Valid mutations succeed
    cfg.name = "renamed_service"
    assert cfg.name == "renamed_service"

    cfg.max_rpc_concurrency = 20
    assert cfg.max_rpc_concurrency == 20

    cfg.max_rpc_concurrency = None
    assert cfg.max_rpc_concurrency is None

    cfg.health_port = 9000
    assert cfg.health_port == 9000

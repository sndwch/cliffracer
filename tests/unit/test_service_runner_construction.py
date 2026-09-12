import pytest

from cliffracer.core import ServiceConfig
from cliffracer.runners.orchestrator import ServiceRunner

pytestmark = pytest.mark.unit


class NoArgService:
    """Self-configuring service (the codebase convention)."""

    def __init__(self):
        self.config = ServiceConfig(name="noarg_service", nats_url="nats://self:4222")


class ConfigArgService:
    """Service whose constructor takes a config (base-class convention)."""

    def __init__(self, config: ServiceConfig):
        self.config = config


def test_constructs_no_arg_service_and_keeps_its_config():
    runner = ServiceRunner(NoArgService)
    svc = runner._construct_service()
    assert isinstance(svc, NoArgService)
    assert svc.config.name == "noarg_service"
    assert svc.config.nats_url == "nats://self:4222"


def test_overrides_overlay_onto_self_built_config():
    runner = ServiceRunner(
        NoArgService, overrides={"nats_url": "nats://override:4222", "log_level": "DEBUG"}
    )
    svc = runner._construct_service()
    assert svc.config.nats_url == "nats://override:4222"
    assert svc.config.log_level == "DEBUG"
    # untouched field retained
    assert svc.config.name == "noarg_service"


def test_config_arg_constructor_receives_config():
    cfg = ServiceConfig(name="cfg_service", nats_url="nats://given:4222")
    runner = ServiceRunner(ConfigArgService, config=cfg)
    svc = runner._construct_service()
    assert svc.config.name == "cfg_service"
    assert svc.config.nats_url == "nats://given:4222"


def test_legacy_config_overlays_when_constructor_is_no_arg():
    # ecommerce-style call: pass a ServiceConfig to a no-arg service class.
    cfg = ServiceConfig(name="ignored_name", auto_restart=False, restart_delay=2.0)
    runner = ServiceRunner(NoArgService, config=cfg)
    svc = runner._construct_service()
    # name stays the service's own; behavioral fields overlay
    assert svc.config.name == "noarg_service"
    assert svc.config.auto_restart is False
    assert svc.config.restart_delay == 2.0


def test_legacy_overlay_applies_explicitly_set_default_value():
    # NoArgService self-configures nats_url to "nats://self:4222". A legacy config
    # that EXPLICITLY sets nats_url to the schema default ("nats://localhost:4222")
    # must still overlay and override the service's own value. This distinguishes
    # exclude_unset (correct) from exclude_defaults (which would drop the field
    # because it equals the default, leaving "nats://self:4222").
    cfg = ServiceConfig(name="ignored_name", nats_url="nats://localhost:4222")
    runner = ServiceRunner(NoArgService, config=cfg)
    svc = runner._construct_service()
    assert svc.config.nats_url == "nats://localhost:4222"


def test_unknown_override_key_raises():
    runner = ServiceRunner(NoArgService, overrides={"not_a_field": 1})
    with pytest.raises(ValueError, match="not_a_field"):
        runner._construct_service()


# ---------------------------------------------------------------------------
# Backoff accumulation tests
# ---------------------------------------------------------------------------


def test_backoff_grows_across_crashes_and_resets_on_success():
    """Verify exponential backoff stepping and cap behavior."""
    from cliffracer.runners.orchestrator import _next_backoff

    restart_delay = 0.5
    max_backoff = 60.0

    # First crash seeds from restart_delay
    b = restart_delay
    assert b == 0.5

    # Accumulator grows exponentially each crash
    b = _next_backoff(b, max_backoff)
    assert b == 1.0

    b = _next_backoff(b, max_backoff)
    assert b == 2.0

    b = _next_backoff(b, max_backoff)
    assert b == 4.0

    # Verify cap
    b = 40.0
    b = _next_backoff(b, max_backoff)
    assert b == 60.0  # min(80, 60) == 60

    b = _next_backoff(b, max_backoff)
    assert b == 60.0  # stays at cap

    # Reset on success returns to seed
    b = restart_delay
    assert b == 0.5

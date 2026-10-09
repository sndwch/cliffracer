import pytest

from cliffracer.core import ServiceConfig
from cliffracer.runners.orchestrator import ServiceRunner
from tests.conftest import broker_url

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
        NoArgService, overrides={"nats_url": "nats://override:4222", "version": "9.9.9"}
    )
    svc = runner._construct_service()
    assert svc.config.nats_url == "nats://override:4222"
    assert svc.config.version == "9.9.9"
    # untouched field retained
    assert svc.config.name == "noarg_service"


class KwargsService:
    """Self-configuring service whose constructor takes only **kwargs."""

    def __init__(self, **kwargs):
        self.config = ServiceConfig(name="kwargs_service", nats_url="nats://self:4222")


class KeywordOnlyService:
    """Self-configuring service whose only parameter is keyword-only."""

    def __init__(self, *, debug: bool = False):
        self.debug = debug
        self.config = ServiceConfig(name="kwonly_service", nats_url="nats://self:4222")


class VarPositionalService:
    """Service that takes its config through *args."""

    def __init__(self, *args):
        self.config = args[0] if args else ServiceConfig(name="varargs_service")


def test_config_arg_constructor_receives_config():
    cfg = ServiceConfig(name="cfg_service", nats_url="nats://given:4222")
    runner = ServiceRunner(ConfigArgService, config=cfg)
    svc = runner._construct_service()
    assert svc.config.name == "cfg_service"
    assert svc.config.nats_url == "nats://given:4222"


@pytest.mark.parametrize(
    ("service_class", "own_name"),
    [(KwargsService, "kwargs_service"), (KeywordOnlyService, "kwonly_service")],
)
def test_a_constructor_with_no_positional_parameter_self_configures(service_class, own_name):
    """**kwargs and a keyword-only parameter each accept no positional argument.

    Such a class self-configures, so the config is overlaid onto the config the
    instance builds rather than handed to the constructor.
    """
    cfg = ServiceConfig(name="ignored_name", restart_delay=3.0)
    runner = ServiceRunner(service_class, config=cfg)
    svc = runner._construct_service()
    assert svc.config.name == own_name
    assert svc.config.restart_delay == 3.0


def test_a_var_positional_constructor_receives_the_config():
    """*args binds a positional argument, so the config is passed through."""
    cfg = ServiceConfig(name="varargs_given")
    runner = ServiceRunner(VarPositionalService, config=cfg)
    svc = runner._construct_service()
    assert svc.config.name == "varargs_given"


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
    # that EXPLICITLY sets nats_url to the schema default (broker_url())
    # must still overlay and override the service's own value. This distinguishes
    # exclude_unset (correct) from exclude_defaults (which would drop the field
    # because it equals the default, leaving "nats://self:4222").
    cfg = ServiceConfig(name="ignored_name", nats_url=broker_url())
    runner = ServiceRunner(NoArgService, config=cfg)
    svc = runner._construct_service()
    assert svc.config.nats_url == broker_url()


def test_unknown_override_key_raises():
    runner = ServiceRunner(NoArgService, overrides={"not_a_field": 1})
    with pytest.raises(ValueError, match="not_a_field"):
        runner._construct_service()


# ---------------------------------------------------------------------------
# Backoff accumulation tests
# ---------------------------------------------------------------------------


def test_next_backoff_doubles_and_caps():
    """The pure step function. The runner's own accumulator -- seeded from
    restart_delay, reset after a successful start, capped at sixty -- is read off
    the delays it asks for in test_service_runner_restart_loop.py."""
    from cliffracer.runners.orchestrator import _next_backoff

    max_backoff = 60.0

    b = _next_backoff(0.5, max_backoff)
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

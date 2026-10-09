"""Unit tests for cliffracer-cyanide extension."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from cliffracer_cyanide import (
    CyanideConfig,
    CyanideDisabledError,
    CyanideError,
    CyanideExtension,
    CyanideFaultError,
)

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.dispatch.pipeline import ExtensionPipeline
from cliffracer.core.extension import ExtensionSetupContext, RejectMessage, WorkerContext

pytestmark = pytest.mark.unit


def test_cyanide_config_defaults():
    """Verify CyanideConfig has safe defaults (disabled by default)."""
    cfg = CyanideConfig()
    assert cfg.enabled is False
    assert cfg.slow_delay == 1.0
    assert cfg.raise_delay == 0.5
    assert cfg.sleep_timeout_duration == 60.0
    assert cfg.mode is None


def test_cyanide_config_env_overrides(monkeypatch):
    """Verify environment variables with CLIFFRACER_CYANIDE_ prefix override config."""
    monkeypatch.setenv("CLIFFRACER_CYANIDE_ENABLED", "true")
    monkeypatch.setenv("CLIFFRACER_CYANIDE_SLOW_DELAY", "2.5")
    monkeypatch.setenv("CLIFFRACER_CYANIDE_RAISE_DELAY", "1.5")
    monkeypatch.setenv("CLIFFRACER_CYANIDE_SLEEP_TIMEOUT_DURATION", "15.0")
    monkeypatch.setenv("CLIFFRACER_CYANIDE_MODE", "slow")

    cfg = CyanideConfig()
    assert cfg.enabled is True
    assert cfg.slow_delay == 2.5
    assert cfg.raise_delay == 1.5
    assert cfg.sleep_timeout_duration == 15.0
    assert cfg.mode == "slow"


def test_cyanide_extension_init_and_binding():
    """Verify CyanideExtension initialization with config and kwargs."""
    ext1 = CyanideExtension(enabled=True, slow_delay=0.1)
    assert ext1.config.enabled is True
    assert ext1.config.slow_delay == 0.1

    cfg = CyanideConfig(enabled=False, raise_delay=0.2)
    ext2 = CyanideExtension(config=cfg)
    assert ext2.config.enabled is False
    assert ext2.config.raise_delay == 0.2

    mock_service = MagicMock()
    bound = ext1.bind(mock_service, "cyanide")
    assert isinstance(bound, CyanideExtension)
    assert bound.service is mock_service
    assert bound.name == "cyanide"
    assert bound.config.enabled is True
    assert bound.fails_closed is True
    assert ext1.fails_closed is True


async def test_gating_disabled_raises_for_all_modes():
    """Verify that all failure mode methods raise CyanideDisabledError when enabled=False."""
    ext = CyanideExtension(config=CyanideConfig(enabled=False))

    with pytest.raises(CyanideDisabledError):
        await ext.slow()

    with pytest.raises(CyanideDisabledError):
        await ext.raise_after_delay()

    with pytest.raises(CyanideDisabledError):
        await ext.sleep_past_timeout()

    raw_msg = MagicMock()
    ctx = WorkerContext(
        kind="rpc",
        subject="test.subject",
        headers={},
        correlation_id="corr-1",
        payload={},
        raw=raw_msg,
    )
    with pytest.raises(CyanideDisabledError):
        await ext.drop_reply(ctx)


async def test_slow_mode_execution():
    """Verify slow failure mode delays execution when enabled."""
    ext = CyanideExtension(enabled=True, slow_delay=0.05)

    start_time = time.monotonic()
    await ext.slow()
    elapsed = time.monotonic() - start_time
    assert elapsed >= 0.04

    # Explicit delay override
    start_time = time.monotonic()
    await ext.slow(delay=0.02)
    elapsed = time.monotonic() - start_time
    assert elapsed >= 0.015

    # Negative delay validation
    with pytest.raises(ValueError):
        await ext.slow(delay=-1.0)


async def test_raise_after_delay_mode_execution():
    """Verify raise_after_delay introduces a delay and raises CyanideFaultError."""
    ext = CyanideExtension(enabled=True, raise_delay=0.03)

    start_time = time.monotonic()
    with pytest.raises(CyanideFaultError) as exc_info:
        await ext.raise_after_delay(message="Custom fault injection")
    elapsed = time.monotonic() - start_time

    assert elapsed >= 0.02
    assert "Custom fault injection" in str(exc_info.value)
    assert isinstance(exc_info.value, CyanideError)

    # Negative delay validation
    with pytest.raises(ValueError):
        await ext.raise_after_delay(delay=-0.5)


async def test_sleep_past_timeout_mode_execution():
    """Verify sleep_past_timeout executes sleep duration."""
    ext = CyanideExtension(enabled=True, sleep_timeout_duration=0.03)

    start_time = time.monotonic()
    await ext.sleep_past_timeout()
    elapsed = time.monotonic() - start_time
    assert elapsed >= 0.025

    # Negative duration validation
    with pytest.raises(ValueError):
        await ext.sleep_past_timeout(duration=-1.0)


async def test_drop_reply_mode_execution():
    """Verify drop_reply replaces raw message respond method with an async no-op."""
    ext = CyanideExtension(enabled=True)

    original_respond = AsyncMock()
    raw_msg = MagicMock()
    raw_msg.respond = original_respond

    ctx = WorkerContext(
        kind="rpc",
        subject="orders.rpc.create",
        headers={},
        correlation_id="corr-123",
        payload={"order_id": "ord-1"},
        raw=raw_msg,
    )

    await ext.drop_reply(ctx)

    assert ctx.data.get("_cyanide_dropped_reply") is True
    assert raw_msg.respond != original_respond

    # Executing the replaced respond method should do nothing
    await raw_msg.respond(b'{"status": "ok"}')
    original_respond.assert_not_called()


async def test_worker_setup_interceptor_with_active_mode():
    """Verify worker_setup executes failure mode configured via active_mode."""
    ext = CyanideExtension(enabled=True)
    raw_msg = MagicMock()
    raw_msg.respond = AsyncMock()
    ctx = WorkerContext(
        kind="rpc",
        subject="test.rpc",
        headers={},
        correlation_id=None,
        payload={},
        raw=raw_msg,
    )

    # Active mode: slow
    ext.set_mode("slow")
    ext.config.slow_delay = 0.02
    start_time = time.monotonic()
    await ext.worker_setup(ctx)
    assert time.monotonic() - start_time >= 0.015

    # Active mode: raise_after_delay
    ext.set_mode("raise_after_delay")
    ext.config.raise_delay = 0.01
    with pytest.raises(CyanideFaultError):
        await ext.worker_setup(ctx)

    # Active mode: drop_reply
    ext.set_mode("drop_reply")
    await ext.worker_setup(ctx)
    assert ctx.data.get("_cyanide_dropped_reply") is True

    # When disabled, active mode is ignored and doesn't raise
    ext.set_enabled(False)
    await ext.worker_setup(ctx)


async def test_worker_setup_interceptor_with_headers():
    """Verify worker_setup responds to incoming request headers."""
    ext = CyanideExtension(enabled=True)
    raw_msg = MagicMock()
    raw_msg.respond = AsyncMock()

    # Header requested drop-reply
    ctx_drop = WorkerContext(
        kind="rpc",
        subject="test.rpc",
        headers={"x-cyanide-mode": "drop-reply"},
        correlation_id=None,
        payload={},
        raw=raw_msg,
    )
    await ext.worker_setup(ctx_drop)
    assert ctx_drop.data.get("_cyanide_dropped_reply") is True

    # Header requested slow
    ctx_slow = WorkerContext(
        kind="rpc",
        subject="test.rpc",
        headers={"x-cyanide-mode": "slow", "x-cyanide-delay": "0.02"},
        correlation_id=None,
        payload={},
        raw=raw_msg,
    )
    start_time = time.monotonic()
    await ext.worker_setup(ctx_slow)
    assert time.monotonic() - start_time >= 0.015

    # Header requested fault
    ctx_fault = WorkerContext(
        kind="rpc",
        subject="test.rpc",
        headers={
            "x-cyanide-fault": "raise-after-delay",
            "x-cyanide-delay": "0.01",
            "x-cyanide-message": "Header injected fault",
        },
        correlation_id=None,
        payload={},
        raw=raw_msg,
    )
    with pytest.raises(CyanideFaultError) as exc_info:
        await ext.worker_setup(ctx_fault)
    assert "Header injected fault" in str(exc_info.value)

    # Header requested fault while disabled
    ext.set_enabled(False)
    await ext.worker_setup(ctx_drop)


async def test_worker_setup_passthrough_when_no_fault_configured():
    """Verify normal requests pass through without error when no fault mode applies."""
    ext = CyanideExtension(enabled=False)
    raw_msg = MagicMock()
    raw_msg.respond = AsyncMock()
    ctx = WorkerContext(
        kind="rpc",
        subject="normal.request",
        headers={},
        correlation_id=None,
        payload={},
        raw=raw_msg,
    )

    # Does not raise even though enabled is False, because no fault was requested
    await ext.worker_setup(ctx)
    assert ctx.data.get("_cyanide_dropped_reply") is None


async def test_worker_setup_handler_specific_configuration():
    """Verify worker_setup can apply faults mapped to specific handler names."""
    ext = CyanideExtension(enabled=True)
    ext.configure_handler("flaky_handler", "raise_after_delay")
    ext.config.raise_delay = 0.01

    ctx = WorkerContext(
        kind="rpc",
        subject="service.flaky_handler",
        headers={},
        correlation_id=None,
        payload={},
        data={"handler_name": "flaky_handler"},
    )
    with pytest.raises(CyanideFaultError):
        await ext.worker_setup(ctx)

    # Different handler has no fault
    ctx_clean = WorkerContext(
        kind="rpc",
        subject="service.healthy_handler",
        headers={},
        correlation_id=None,
        payload={},
        data={"handler_name": "healthy_handler"},
    )
    await ext.worker_setup(ctx_clean)


async def test_lifecycle_and_health_reporting():
    """Verify lifecycle hooks and health/info details."""
    ext = CyanideExtension(enabled=True)
    mock_service = MagicMock()
    setup_ctx = ExtensionSetupContext(
        service_config=ServiceConfig(name="test-service"),
        broker_url="nats://dummy",
        service=mock_service,
    )

    await ext.setup(setup_ctx)
    assert ext.service is mock_service

    await ext.start()
    await ext.stop()

    health = ext.health_details()
    assert health == {
        "enabled": True,
        "mode": None,
        "seed": ext.config.seed,
        "injections_recorded": 0,
        "injections_dropped": 0,
        "injection_record_limit": ext.config.injection_record_limit,
    }

    # info_details stays configuration only. health is where a reader looks for
    # what has happened; info is what the service was asked to be.
    info = ext.info_details()
    assert info == {"enabled": True, "mode": None, "seed": ext.config.seed}


async def test_service_composition_and_mro_isolation():
    """Verify CyanideExtension composes cleanly on CliffracerService without joining MRO."""

    class ExampleService(CliffracerService):
        cyanide = CyanideExtension(enabled=True, slow_delay=0.02)

        @rpc
        async def sample_rpc(self, val: int) -> dict[str, int]:
            await self.cyanide.slow()
            return {"result": val * 2}

    # Extension is not in service class MRO
    assert CyanideExtension not in ExampleService.__mro__

    svc = ExampleService(ServiceConfig(name="example-service"))
    await svc.container._setup_extensions()

    # Bound extension is instance of CyanideExtension
    assert isinstance(svc.cyanide, CyanideExtension)
    assert svc.cyanide.config.enabled is True

    # Direct handler call exercises the extension
    start_time = time.monotonic()
    result = await svc.sample_rpc(5)
    assert result == {"result": 10}
    assert time.monotonic() - start_time >= 0.015


def test_exception_type_hierarchy():
    """Verify typed exceptions inherit from CyanideError."""
    assert issubclass(CyanideFaultError, CyanideError)
    assert issubclass(CyanideDisabledError, CyanideError)

    try:
        raise CyanideFaultError("fault")
    except CyanideError as err:
        assert str(err) == "fault"

    try:
        raise CyanideDisabledError("disabled")
    except CyanideError as err:
        assert str(err) == "disabled"


async def test_worker_setup_pipeline_halts_handler_when_fails_closed():
    """Verify that running an ExtensionPipeline with CyanideExtension raises RejectMessage and halts handler."""
    ext = CyanideExtension(enabled=True, mode="raise_after_delay", raise_delay=0.001)
    assert ext.fails_closed is True

    pipeline = ExtensionPipeline([ext])
    ctx = WorkerContext(
        kind="rpc",
        subject="test.subject",
        headers={},
        correlation_id="corr-1",
        payload={},
        raw=MagicMock(),
    )
    handler = AsyncMock()

    with pytest.raises(RejectMessage):
        await pipeline.run_worker(ctx, handler)

    handler.assert_not_called()

    # Also verify when bound to a service
    mock_service = MagicMock()
    bound = ext.bind(mock_service, "cyanide")
    assert bound.fails_closed is True

    pipeline_bound = ExtensionPipeline([bound])
    handler_bound = AsyncMock()

    with pytest.raises(RejectMessage):
        await pipeline_bound.run_worker(ctx, handler_bound)

    handler_bound.assert_not_called()


async def test_a_disabled_extension_ignores_a_mode_requested_by_header():
    """A caller must not be able to inject a fault into a service with cyanide off.

    `x-cyanide-mode` comes off the wire, so a disabled extension has to be inert
    rather than merely non-raising: the header is attacker-chosen, and both a
    fault and an error are outcomes the caller should not be able to select.
    """
    ext = CyanideExtension(config=CyanideConfig(enabled=False))
    raw = MagicMock()
    original_respond = raw.respond
    ctx = WorkerContext(
        kind="rpc",
        subject="orders.create",
        headers={"x-cyanide-mode": "drop_reply"},
        correlation_id="c1",
        payload={},
        raw=raw,
    )

    await ext.worker_setup(ctx)

    assert ctx.data.get("_cyanide_dropped_reply") is None, "the fault ran while disabled"
    assert raw.respond is original_respond, "the reply was suppressed while disabled"

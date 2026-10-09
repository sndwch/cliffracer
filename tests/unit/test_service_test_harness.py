"""Tests for ServiceTestHarness and CliffracerService delegation deprecation."""

import asyncio
from typing import Any

import pytest

from cliffracer.core.container import DispatchOutcome
from cliffracer.core.decorators import listener, rpc
from cliffracer.core.extension import Extension, ExtensionSetupContext
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.testing import MockMessage, ServiceTestHarness, TestResponse

pytestmark = pytest.mark.unit


class CalculatorService(CliffracerService):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.received_events: list[dict[str, Any]] = []

    @rpc
    def add(self, a: int, b: int) -> int:
        return a + b

    @rpc
    def divide(self, a: float, b: float) -> float:
        if b == 0:
            raise ValueError("division by zero")
        return a / b

    @listener("calc.event", fanout=True)
    async def on_event(self, action: str = "") -> None:
        self.received_events.append({"action": action})


async def test_harness_rpc_call_success():
    """ServiceTestHarness executes typed RPC calls and returns decoded TestResponse."""
    async with ServiceTestHarness(CalculatorService) as harness:
        resp = await harness.rpc("add", a=10, b=25)
        assert resp.success is True
        assert resp.result == 35
        assert resp.error is None
        # The request's own type, written by `harness.rpc()`; see the adversarial file.
        assert resp.headers.get("Content-Type") == "application/json"


async def test_harness_rpc_validation_error():
    """ServiceTestHarness handles invalid payloads and captures validation errors."""
    async with ServiceTestHarness(CalculatorService) as harness:
        resp = await harness.rpc("add", a="not_an_integer")
        assert resp.success is False
        assert resp.error is not None
        assert "validation" in resp.error.lower() or "input" in resp.error.lower()


async def test_harness_emit_event():
    """ServiceTestHarness delivers events through container dispatch pipeline."""
    async with ServiceTestHarness(CalculatorService) as harness:
        outcome = await harness.emit_event("calc.event", {"action": "reset"})
        assert outcome == DispatchOutcome.OK
        svc = harness.service
        assert isinstance(svc, CalculatorService)
        assert len(svc.received_events) == 1
        assert svc.received_events[0] == {"action": "reset"}


async def test_harness_describe():
    """ServiceTestHarness queries service describe endpoint."""
    async with ServiceTestHarness(CalculatorService) as harness:
        desc = await harness.describe()
        assert desc["service"] == "test_harness_svc"
        assert "methods" in desc
        assert any(m["name"] == "add" for m in desc["methods"])
        assert any(m["name"] == "divide" for m in desc["methods"])


async def test_mock_message_interaction():
    """MockMessage supports response, ack, nak, and term operations."""
    msg = MockMessage(subject="test.subject", data=b'{"hello": "world"}')
    assert msg.subject == "test.subject"
    assert msg.data == b'{"hello": "world"}'

    await msg.respond(b'{"result": 1}')
    assert msg.responded_data == b'{"result": 1}'

    await msg.ack()
    assert msg.acked is True

    nakked = MockMessage(subject="test.subject")
    await nakked.nak(delay=1.5)
    assert nakked.nacked is True
    assert nakked.nak_delay == 1.5

    terminated = MockMessage(subject="test.subject")
    await terminated.term()
    assert terminated.terminated is True


async def test_delegations_removed_from_service():
    """Private delegation methods are removed from CliffracerService and raise AttributeError."""
    cfg = ServiceConfig(name="depr_svc", health_port=0)
    svc = CalculatorService(cfg)

    for attr in [
        "_on_rpc_request",
        "_handle_rpc_request",
        "_on_describe_request",
        "_on_async_request",
        "_with_namespace",
        "_make_event_callback",
    ]:
        assert not hasattr(svc, attr)
        with pytest.raises(AttributeError):
            getattr(svc, attr)


def test_test_response_unanswered_message_evaluates_to_unsuccessful():
    """An unanswered MockMessage produces a TestResponse with success=False."""
    msg = MockMessage(subject="orders.rpc.process")
    resp = TestResponse.from_mock_message(msg)

    assert resp.success is False
    assert resp.raw_data == b""
    assert resp.data is None
    assert resp.error is None


def test_test_response_explicit_error_evaluates_to_unsuccessful():
    """An answered MockMessage with an error payload evaluates to success=False."""
    msg = MockMessage(subject="orders.rpc.process")
    msg.responded_data = b'{"error": "Internal server error", "success": false}'
    msg.response_headers = {"Content-Type": "application/json"}
    resp = TestResponse.from_mock_message(msg)

    assert resp.success is False
    assert resp.error == "Internal server error"


class SelfConfiguringService(CliffracerService):
    """Service that builds its own config, the convention the runner documents."""

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="self_configuring_svc", health_port=0))

    @rpc
    def echo(self, value: str) -> str:
        return value


async def test_harness_runs_a_self_configuring_service_class():
    """A no-arg service class is constructed by the harness and its handlers dispatch."""
    async with ServiceTestHarness(SelfConfiguringService) as harness:
        assert harness.service.config.name == "self_configuring_svc"
        resp = await harness.rpc("echo", value="hi")
        assert resp.success is True
        assert resp.result == "hi"


def test_harness_overlays_a_config_onto_a_self_configuring_service():
    """A config given for a no-arg class overlays its fields, and the name stays its own."""
    cfg = ServiceConfig(name="ignored", health_port=0, expose_internal_errors=True)
    harness = ServiceTestHarness(SelfConfiguringService, config=cfg)

    assert harness.service.config.name == "self_configuring_svc"
    assert harness.service.config.expose_internal_errors is True


def test_harness_refuses_a_config_for_an_already_built_service():
    """config= configures a class the harness builds, so it is refused rather than dropped."""
    svc = CalculatorService(ServiceConfig(name="built_svc", health_port=0))

    with pytest.raises(TypeError, match="already-built instance"):
        ServiceTestHarness(svc, config=ServiceConfig(name="wanted", health_port=0))


class NamespacedAuditService(CliffracerService):
    """Service in a namespace, listening on the subject it declares."""

    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.seen: list[dict[str, Any]] = []

    @listener("banking.audit", fanout=True)
    async def on_audit(self, action: str = "") -> None:
        self.seen.append({"action": action})


class LifecycleRecordingExtension(Extension):
    """Extension recording which of its lifecycle hooks ran."""

    def __init__(self) -> None:
        super().__init__()
        self.hooks_run: list[str] = []

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        self.hooks_run.append("setup")

    async def start(self) -> None:
        self.hooks_run.append("start")

    async def stop(self) -> None:
        self.hooks_run.append("stop")


class ExtensionLifecycleService(CliffracerService):
    """Service carrying an extension that records its lifecycle."""

    recorder: LifecycleRecordingExtension = LifecycleRecordingExtension()

    @rpc
    def ping(self) -> str:
        return "pong"


class StuckTaskService(CliffracerService):
    """Service whose handler spawns a supervised task that never finishes."""

    @rpc
    async def spawn_stuck_worker(self) -> dict[str, str]:
        async def _never_finishes() -> None:
            await asyncio.sleep(3600)

        self.container.lifecycle.spawn_supervised_task(_never_finishes(), name="stuck")
        return {"status": "spawned"}


async def test_harness_emit_event_namespaces_the_subject():
    """A namespaced service receives the subject written in its own @listener."""
    cfg = ServiceConfig(name="ns_audit_svc", health_port=0, namespace="tenantA")
    async with ServiceTestHarness(NamespacedAuditService, config=cfg) as harness:
        outcome = await harness.emit_event("banking.audit", {"action": "login"})

        assert outcome == DispatchOutcome.OK
        svc = harness.service
        assert isinstance(svc, NamespacedAuditService)
        assert svc.seen == [{"action": "login"}]


async def test_harness_runs_the_three_extension_lifecycle_hooks():
    """setup, start and stop all run, in the order a live start runs them."""
    harness = ServiceTestHarness(ExtensionLifecycleService)
    async with harness:
        ext = next(
            e for e in harness.container.extensions if isinstance(e, LifecycleRecordingExtension)
        )
        resp = await harness.rpc("ping")
        assert resp.success is True
        assert ext.hooks_run == ["setup", "start"]

    assert ext.hooks_run == ["setup", "start", "stop"]


async def test_harness_refuses_to_dispatch_after_teardown():
    """A call after teardown fails rather than silently setting the service up again."""
    harness = ServiceTestHarness(CalculatorService)
    async with harness:
        resp = await harness.rpc("add", a=1, b=2)
        assert resp.success is True

    with pytest.raises(RuntimeError, match="torn down"):
        await harness.rpc("add", a=1, b=2)


async def test_harness_teardown_cancels_a_task_that_outlasts_the_drain():
    """A task that never finishes is cancelled at the deadline, not waited on forever."""
    harness = ServiceTestHarness(StuckTaskService)
    await harness.setup()
    resp = await harness.rpc("spawn_stuck_worker")
    assert resp.success is True
    assert len(harness.container.lifecycle.active_tasks) == 1

    await asyncio.wait_for(harness.teardown(drain_timeout=0.05), timeout=5.0)

    assert len(harness.container.lifecycle.active_tasks) == 0

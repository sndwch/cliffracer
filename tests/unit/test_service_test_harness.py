"""Tests for ServiceTestHarness and CliffracerService delegation deprecation."""

from typing import Any

import pytest

from cliffracer.core.container import DispatchOutcome
from cliffracer.core.decorators import listener, rpc
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.testing import MockMessage, ServiceTestHarness

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

    await msg.nak(delay=1.5)
    assert msg.nacked is True
    assert msg.nak_delay == 1.5

    await msg.term()
    assert msg.terminated is True


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

"""A gate that crashes is a fault on a timer firing and on an async request, as it is everywhere else.

A refusal is the caller being turned away and a fault is the service being broken, and ADR-0006 sends
the two to different people. RPC, events, describe and metrics already tell a gate that refused from
one that crashed (`RejectMessage.hook_crash`). The timer and the fire-and-forget request took every
`RejectMessage` for a refusal, so an auth backend that was down made every timer firing "refused"
with `error_count` 0 (an alarm on the error rate stayed quiet) and logged every async request at
WARNING as "refused".
"""

from __future__ import annotations

import json

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, async_rpc, timer
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit


class Gate(Extension):
    """A gate that fails closed and, when told to, crashes instead of deciding."""

    fails_closed = True

    async def worker_setup(self, ctx):
        mode = getattr(self.service, "gate_does", "admit")
        if mode == "crash":
            raise RuntimeError("the auth backend is down")
        if mode == "refuse":
            raise RejectMessage("unauthenticated")


class Svc(CliffracerService):
    gate = Gate()

    def __init__(self, config):
        super().__init__(config)
        self.gate_does = "admit"
        self.ran = 0

    @timer(interval=0.01)
    async def tick(self):
        self.ran += 1

    @async_rpc
    async def audit(self) -> None:
        self.ran += 1


async def _service():
    svc = Svc(ServiceConfig(name="s", health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    return svc


def _capture():
    lines: list[tuple[str, str, bool]] = []
    sink = logger.add(
        lambda m: lines.append(
            (m.record["level"].name, m.record["message"], m.record["exception"] is not None)
        ),
        level="DEBUG",
    )
    return lines, sink


async def _fire_timer(svc: Svc):
    t = svc._timers[0]
    t.service_instance = svc
    t.method_name = "tick"
    lines, sink = _capture()
    try:
        await t._execute_method()
    finally:
        logger.remove(sink)
    return t, lines


async def _send_async(svc: Svc):
    msg = MockMessage(
        "s.async.audit", data=json.dumps({}).encode(), headers={"Content-Type": "application/json"}
    )
    lines, sink = _capture()
    try:
        await svc.container._handle_async_request(msg)
    finally:
        logger.remove(sink)
    return lines


async def test_a_timer_firing_stopped_by_a_crashed_gate_is_an_error_not_a_refusal():
    svc = await _service()
    svc.gate_does = "crash"

    t, lines = await _fire_timer(svc)

    assert svc.ran == 0, "the method must not run behind a gate that crashed"
    assert (t.error_count, t.refusal_count, t.execution_count) == (1, 0, 1)
    assert t.last_error is not None and t.last_error.startswith("RejectMessage:")
    assert t.last_error_type == "RejectMessage"
    assert t.last_refusal is None
    errors = [(msg, tb) for level, msg, tb in lines if level == "ERROR" and "tick" in msg]
    assert len(errors) == 1 and errors[0][1] is True, lines
    assert t.get_stats()["error_rate"] == 100.0
    assert not [1 for level, msg, _ in lines if level == "WARNING" and "refused" in msg], lines


async def test_CONTROL_a_timer_firing_a_gate_refuses_is_still_a_refusal():
    svc = await _service()
    svc.gate_does = "refuse"

    t, lines = await _fire_timer(svc)

    assert (t.error_count, t.refusal_count, t.execution_count) == (0, 1, 0)
    assert (t.last_error, t.last_refusal) == (None, "unauthenticated")
    assert [(level, msg) for level, msg, _ in lines if level in ("WARNING", "ERROR")] == [
        ("WARNING", "Timer method tick refused: unauthenticated")
    ]


async def test_an_async_request_stopped_by_a_crashed_gate_is_logged_as_an_error():
    svc = await _service()
    svc.gate_does = "crash"

    lines = await _send_async(svc)

    assert svc.ran == 0
    errors = [msg for level, msg, _ in lines if level == "ERROR" and "audit" in msg]
    assert len(errors) == 1 and "extension hook crashed" in errors[0], lines
    assert not [1 for level, msg, _ in lines if level == "WARNING" and "refused" in msg], lines


async def test_CONTROL_an_async_request_a_gate_refuses_is_still_a_refusal():
    svc = await _service()
    svc.gate_does = "refuse"

    lines = await _send_async(svc)

    assert svc.ran == 0
    warnings = [msg for level, msg, _ in lines if level == "WARNING" and "audit" in msg]
    assert len(warnings) == 1 and "refused" in warnings[0] and "unauthenticated" in warnings[0]
    assert not [1 for level, msg, _ in lines if level == "ERROR" and "audit" in msg], lines


async def test_CONTROL_a_gate_that_admits_runs_the_method_on_both_paths():
    svc = await _service()

    await _fire_timer(svc)
    await _send_async(svc)

    assert svc.ran == 2

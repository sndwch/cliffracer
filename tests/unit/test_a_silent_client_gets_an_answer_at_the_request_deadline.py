"""A client that connects and says nothing is answered when the request deadline passes.

The probe must get an answer: a handler that waits for a request line that never comes ends at
the deadline with a response, and the body carries no exception text. Nothing exercised that:
removing the catch-all that writes the answer made a silent client get an empty connection close
after the deadline and no test noticed, and nothing checked what the body held. The deadline is
shortened here (the module constant is read per request) so the test takes a fraction of a second.
"""

import asyncio
import json

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core import health_listener as health_listener_module
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit

DEADLINE = 0.3


async def _silent_client(expose: bool) -> tuple[int, dict, float]:
    svc = CliffracerService(
        ServiceConfig(name="silent", health_port=0, expose_internal_errors=expose)
    )
    listener = HealthListener(svc, "127.0.0.1", 0)
    await listener.start()
    loop = asyncio.get_running_loop()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        started = loop.time()
        raw = await asyncio.wait_for(reader.read(), timeout=DEADLINE + 5.0)  # sends nothing
        waited = loop.time() - started
        writer.close()
    finally:
        await listener.stop()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body), waited


async def test_a_silent_client_gets_a_500_after_the_deadline_not_a_dropped_connection(monkeypatch):
    monkeypatch.setattr(health_listener_module, "REQUEST_DEADLINE_SECONDS", DEADLINE)

    status, body, waited = await _silent_client(expose=False)

    assert status == 500, (status, body)
    # Upper bound. CI p99 0.302 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.3 s,
    # 1901x the overshoot; below 5 s (the unpatched 5.0 s deadline).
    # Lower bound: 80% of the deadline; an answer sent without waiting for it falls under it. Load
    # can only lengthen it.
    assert DEADLINE * 0.8 <= waited < DEADLINE + 3.0, waited
    assert "error" in body


async def test_the_answer_to_a_silent_client_carries_no_exception_text_by_default(monkeypatch):
    monkeypatch.setattr(health_listener_module, "REQUEST_DEADLINE_SECONDS", DEADLINE)

    _, body, _ = await _silent_client(expose=False)

    assert "TimeoutError" not in json.dumps(body), body


async def test_CONTROL_with_the_switch_on_the_text_is_allowed_to_appear(monkeypatch):
    monkeypatch.setattr(health_listener_module, "REQUEST_DEADLINE_SECONDS", DEADLINE)

    _, body, _ = await _silent_client(expose=True)

    assert "TimeoutError" in json.dumps(body), body

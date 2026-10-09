"""A service that starts and then stops leaves no connection of its own on the broker.

This reads the broker's own monitoring endpoint (`/connz`), which is not the port the suite's
broker probe checks, so a run against a broker with no monitoring cannot say anything about
leaks. In the unit tier that meant the test skipped itself when `localhost:8222` did not answer:
a networked test that a plain `pytest -m unit` included, and that disappeared into a skip on
any machine without monitoring. It belongs with the other tests that need a live broker, and
here an unreachable monitoring endpoint FAILS, naming what to start or set, because a leak check
that cannot reach what it checks has not checked anything.

`CLIFFRACER_TEST_NATS_MONITOR_URL` names the endpoint (the private-broker wrapper and the CI
broker both provide it); without it the default is `http://localhost:8222`, which `nats-server
-m 8222` serves. `tests/unit/test_the_live_leak_check_reads_the_monitor_it_is_given.py` drives
this check with a stub service and a stub `urlopen`, so its monitoring handling is tested without
a broker.
"""

import asyncio
import json
import os
import urllib.request

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.decorators import listener, rpc
from tests.conftest import broker_url

pytestmark = pytest.mark.integration

SERVICE_NAME = "live_leak_check"


class LeakCheckService(CliffracerService):
    """A service with an RPC handler and a listener, so it opens real subscriptions."""

    @rpc
    async def echo_fast(self, msg: str) -> str:
        return msg

    @listener("events.leak_check", fanout=True)
    async def on_event(self, count: int) -> None:
        pass


def _connz_url() -> str:
    monitor_url = os.getenv("CLIFFRACER_TEST_NATS_MONITOR_URL", "http://localhost:8222")
    return f"{monitor_url.rstrip('/')}/connz"


def _read_connz(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        raise AssertionError(
            f"the broker's monitoring endpoint {url} is not reachable ({exc}). This check needs "
            f"it: start nats-server with -m 8222, or set CLIFFRACER_TEST_NATS_MONITOR_URL"
        ) from exc


def _named(connz: dict) -> list[str]:
    return [c["name"] for c in connz.get("connections", []) if c.get("name") == SERVICE_NAME]


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_a_started_and_stopped_service_leaves_no_connection_on_the_broker() -> None:
    connz_url = _connz_url()
    _read_connz(connz_url)

    svc = LeakCheckService(ServiceConfig(name=SERVICE_NAME, nats_url=broker_url()))

    try:
        await svc.start()
        assert bool(getattr(svc.container.lifecycle, "_running"))  # noqa: B009
        assert svc.is_broker_connected
        assert _named(_read_connz(connz_url)) == [SERVICE_NAME]
    finally:
        await svc.stop()
    assert not bool(getattr(svc.container.lifecycle, "_running"))  # noqa: B009

    await asyncio.sleep(0.1)
    assert _named(_read_connz(connz_url)) == []

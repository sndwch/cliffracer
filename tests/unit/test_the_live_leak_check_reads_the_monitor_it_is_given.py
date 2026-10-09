"""The live leak check reads the monitoring endpoint it is given, always stops its service, and
fails, not skips, when the endpoint is unreachable.

`tests/integration/test_a_started_and_stopped_service_leaves_no_connection_on_the_broker.py` is
the check itself and needs a broker. These drive the same function with a stub service and a stub
`urlopen`, so what it does with the monitoring endpoint is tested in the unit tier, where no
broker is available to make it pass by accident. The function is reached through its module, not
imported by name, so it is collected once, in the tier that owns it.
"""

import io
import json
import urllib.parse
from types import SimpleNamespace

import pytest

from tests.integration import (
    test_a_started_and_stopped_service_leaves_no_connection_on_the_broker as leak,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("connection_visible", [True, False])
async def test_the_check_observes_the_selected_broker_and_always_stops(
    monkeypatch, connection_visible
):
    selected = "http://127.0.0.1:18999/"
    monkeypatch.setenv("CLIFFRACER_TEST_NATS_MONITOR_URL", selected)
    lifecycle = SimpleNamespace(_running=False)
    service = SimpleNamespace(
        container=SimpleNamespace(lifecycle=lifecycle), is_broker_connected=True
    )

    async def start() -> None:
        lifecycle._running = True

    async def stop() -> None:
        lifecycle._running = False

    service.start = start
    service.stop = stop
    stops: list[bool] = []

    async def counted_stop() -> None:
        stops.append(True)
        await stop()

    service.stop = counted_stop
    monkeypatch.setattr(leak, "LeakCheckService", lambda config: service)
    observed_urls = []

    def read_monitor(url, timeout):
        observed_urls.append(url)
        connections = (
            [{"name": leak.SERVICE_NAME}] if lifecycle._running and connection_visible else []
        )
        return io.BytesIO(json.dumps({"connections": connections}).encode())

    monkeypatch.setattr("urllib.request.urlopen", read_monitor)
    if connection_visible:
        await leak.test_a_started_and_stopped_service_leaves_no_connection_on_the_broker()
    else:
        with pytest.raises(AssertionError):
            await leak.test_a_started_and_stopped_service_leaves_no_connection_on_the_broker()
    assert stops == [True]
    assert not lifecycle._running
    assert observed_urls == [f"{selected}connz"] * (3 if connection_visible else 2)


@pytest.mark.parametrize("monitor_url", ["http://127.0.0.1:18999", None], ids=["named", "default"])
async def test_an_unreachable_monitoring_endpoint_fails_and_says_what_to_set(
    monkeypatch, monitor_url
):
    if monitor_url is None:
        monkeypatch.delenv("CLIFFRACER_TEST_NATS_MONITOR_URL", raising=False)
        monkeypatch.setattr(leak, "broker_url", lambda: leak.DEFAULT_BROKER_URL)
    else:
        monkeypatch.setenv("CLIFFRACER_TEST_NATS_MONITOR_URL", monitor_url)

    def unreachable(url, timeout):
        raise OSError("monitor unavailable")

    monkeypatch.setattr("urllib.request.urlopen", unreachable)

    try:
        with pytest.raises(AssertionError, match="CLIFFRACER_TEST_NATS_MONITOR_URL") as raised:
            await leak.test_a_started_and_stopped_service_leaves_no_connection_on_the_broker()
    except pytest.skip.Exception:
        pytest.fail("an unreachable monitoring endpoint must fail the check, not skip it")

    assert "monitor unavailable" in str(raised.value)


# A broker on the default host at another port, a broker on another host at the default port, and
# one whose URL carries credentials. Each is built from the default, so none is an address of its own.
ELSEWHERE = [
    f"{leak.DEFAULT_BROKER_URL}0",
    leak.DEFAULT_BROKER_URL.replace("localhost", "nats.example"),
    leak.DEFAULT_BROKER_URL.replace("//", "//user:pw@").replace("localhost", "nats.example"),
]


@pytest.mark.parametrize("broker", ELSEWHERE, ids=["another-port", "another-host", "credentials"])
async def test_a_broker_elsewhere_with_no_monitor_named_fails_by_name_and_reads_nothing(
    monkeypatch, broker
):
    monkeypatch.delenv("CLIFFRACER_TEST_NATS_MONITOR_URL", raising=False)
    monkeypatch.setattr(leak, "broker_url", lambda: broker)
    read: list[str] = []
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout: read.append(url))

    with pytest.raises(AssertionError) as raised:
        await leak.test_a_started_and_stopped_service_leaves_no_connection_on_the_broker()

    where = urllib.parse.urlsplit(broker)
    assert f"the broker under test is at {where.hostname}:{where.port} " in str(raised.value)
    assert "Set CLIFFRACER_TEST_NATS_MONITOR_URL" in str(raised.value)
    assert "pw" not in str(raised.value)
    assert read == []


def test_CONTROL_the_suites_default_broker_keeps_the_default_monitor(monkeypatch):
    monkeypatch.delenv("CLIFFRACER_TEST_NATS_MONITOR_URL", raising=False)
    monkeypatch.setattr(leak, "broker_url", lambda: leak.DEFAULT_BROKER_URL)

    assert leak._connz_url() == f"{leak.DEFAULT_MONITOR_URL}/connz"

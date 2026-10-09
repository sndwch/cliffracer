"""Adversarial stress tests for HealthListener Kubernetes probe segregation.

Empirically challenges:
1. Probe segregation under degraded downstream dependencies (exceptions, timeouts, partial outages).
2. Probe segregation under broker failures (CONNECTING, DISCONNECTED, CLOSED, dynamic flapping).
3. Probe responses under stopped service state (before start, after stop).
4. Backward compatibility parity between /health and /ready across all operational states.
5. HTTP routing security: non-GET methods (POST, PUT, DELETE, PATCH, OPTIONS) returning 405,
   invalid paths returning 404, and resilience to malformed HTTP streams.
6. Non-blocking concurrency isolation: slow/failing /ready checks do not block concurrent /live probes.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener
from cliffracer.testing import skip_if_the_host_is_too_busy_to_judge

pytestmark = pytest.mark.unit


async def _raw_request(
    port: int,
    path: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | bytes = "",
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Execute raw HTTP request over TCP socket, returning (status, headers, parsed_json_or_empty)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    req_headers = {"Host": "localhost", "Connection": "close"}
    if headers:
        req_headers.update(headers)
    if isinstance(body, str):
        body_bytes = body.encode()
    else:
        body_bytes = body
    if body_bytes and "Content-Length" not in req_headers:
        req_headers["Content-Length"] = str(len(body_bytes))

    header_lines = "".join(f"{k}: {v}\r\n" for k, v in req_headers.items())
    req = f"{method} {path} HTTP/1.1\r\n{header_lines}\r\n".encode() + body_bytes
    writer.write(req)
    await writer.drain()

    raw = await reader.read()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass

    head, _, resp_body = raw.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    status = int(lines[0].split(" ")[1])
    resp_headers: dict[str, str] = {}
    for line in lines[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            resp_headers[k.lower()] = v

    parsed: dict[str, Any] = {}
    if resp_body:
        try:
            parsed = json.loads(resp_body)
        except Exception:
            parsed = {"raw": resp_body.decode(errors="replace")}
    return status, resp_headers, parsed


def _simulate_service_state(
    svc: CliffracerService,
    *,
    running: bool = True,
    broker_state: str = "connected",
) -> None:
    """Configure mock broker and lifecycle state on service instance."""
    svc._running = running
    if broker_state == "connected":
        svc.nc = type(
            "MockNC",
            (),
            {
                "is_closed": False,
                "is_connected": True,
                "is_draining": False,
                "is_connecting": False,
            },
        )()
    elif broker_state == "connecting":
        svc.nc = type(
            "MockNC",
            (),
            {
                "is_closed": False,
                "is_connected": False,
                "is_draining": False,
                "is_connecting": True,
            },
        )()
    elif broker_state == "disconnected":
        svc.nc = type(
            "MockNC",
            (),
            {
                "is_closed": False,
                "is_connected": False,
                "is_draining": False,
                "is_connecting": False,
            },
        )()
    elif broker_state == "draining":
        # A drain begins on a live connection: `is_connected` is still True while it runs.
        svc.nc = type(
            "MockNC",
            (),
            {
                "is_closed": False,
                "is_connected": True,
                "is_draining": True,
                "is_connecting": False,
            },
        )()
    elif broker_state == "closed":
        svc.nc = type(
            "MockNC",
            (),
            {
                "is_closed": True,
                "is_connected": False,
                "is_draining": False,
                "is_connecting": False,
            },
        )()
    else:
        svc.nc = None


@pytest.mark.parametrize("expose", [True, False], ids=["text-exposed", "text-withheld"])
async def test_health_listener_dependency_exceptions_segregation(expose):
    """Verify that multiple explosive downstream dependencies degrade /ready but never /live.

    Run with the failure text exposed and withheld, which is the default. The segregation, the
    list of unhealthy dependencies and each dependency's `ok` are the same under both. The text
    is what differs: `expose_internal_errors=True` is what lets this assert that each failure is
    attributed to ITS OWN dependency, a claim about the text, and the endpoint withholds that text
    by default -- see `test_the_health_endpoint_withholds_exception_text.py` -- so under the
    default the error does not name the exception.
    """
    svc = CliffracerService(
        ServiceConfig(name="orders_svc", health_port=0, expose_internal_errors=expose)
    )
    _simulate_service_state(svc, running=True, broker_state="connected")

    async def broken_db() -> None:
        raise ConnectionRefusedError("Database 10.0.0.5:5432 unreachable")

    async def broken_vault() -> None:
        raise RuntimeError("Vault token expired")

    async def healthy_cache() -> bool:
        return True

    svc.add_dependency("db", broken_db)
    svc.add_dependency("vault", broken_vault)
    svc.add_dependency("cache", healthy_cache)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        # /live must return 200 OK
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"
        assert live_body["service"] == "orders_svc"
        assert "dependencies" not in live_body

        # /ready must return 503 Service Unavailable
        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "unhealthy"
        assert ready_body["service"] == "orders_svc"
        assert sorted(ready_body["unhealthy_dependencies"]) == ["db", "vault"]
        assert ready_body["dependencies"]["cache"]["ok"] is True
        assert ready_body["dependencies"]["db"]["ok"] is False
        assert ("ConnectionRefusedError" in ready_body["dependencies"]["db"]["error"]) is expose

        # /health must mirror /ready exactly (excluding dynamic timestamp and latency)
        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        assert health_body["status"] == ready_body["status"]
        assert health_body["service"] == ready_body["service"]
        assert health_body.get("unhealthy_dependencies") == ready_body.get("unhealthy_dependencies")
        assert health_body.keys() == ready_body.keys()
    finally:
        await hl.stop()


# What the three numbers below discriminate between.
#
# A `/live` that answered from the dependency check would take DEPENDENCY_TIMEOUT
# to answer, because that is when the hanging dependency gives up. A `/live` that
# does not look at dependencies answers in the cost of a request, which on a busy
# container is a few tens of milliseconds and on a cold listener a little more.
#
# There is no ceiling on /live here any more. It was 0.5s against a timeout of
# 1.0 -- a margin of two on a shared runner -- for a property that is about
# which path answered, not how long it took. The dependency records whether it
# was consulted instead, which reads the same on any host.
DEPENDENCY_TIMEOUT = 1.0
DEPENDENCY_HANG = 5.0


async def test_health_listener_dependency_timeout_segregation():
    """Verify that hanging downstream dependencies trigger timeout in /ready without stalling /live."""
    svc = CliffracerService(ServiceConfig(name="slow_dep_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    consulted: list[str] = []

    async def hanging_payment_gateway() -> None:
        consulted.append("payment_gw")
        await asyncio.sleep(DEPENDENCY_HANG)

    svc.add_dependency("payment_gw", hanging_payment_gateway, timeout=DEPENDENCY_TIMEOUT)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        # A first request against a fresh listener, kept now for a different
        # reason than it was added for. It existed to keep the accept path and
        # its imports out of a timing budget; there is no budget any more. What
        # it buys is coverage: `consulted` accumulates across both requests, so
        # the assertion below covers a cold /live and a warm one, and a listener
        # that consulted dependencies only on its first request would be caught.
        warm_status, _, _ = await _raw_request(hl.port, "/live")
        assert warm_status == 200

        # /live answers without consulting the hanging dependency.
        #
        # Read from the dependency itself rather than from a clock. This was
        # `t_live < LIVE_CEILING`, with the ceiling at 0.5s against a
        # DEPENDENCY_TIMEOUT of 1.0 -- a margin of two, on a runner shared with
        # everything else on the host, for a property that is not about duration
        # at all. Whether the dependency was consulted is a fact the dependency
        # can report, and it reports it the same way on any host.
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"
        assert consulted == [], (
            f"/live consulted {consulted}, so it answered on the dependency path "
            "rather than on its own"
        )

        # /ready waits for timeout and reports 503
        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "unhealthy"

        # The other direction, so `consulted == []` above is not vacuous: a
        # dependency nothing ever calls would satisfy it whatever /live did.
        assert consulted == ["payment_gw"], (
            f"/ready did not consult the dependency ({consulted}), so the "
            "assertion that /live did not consult it says nothing"
        )
        assert "payment_gw" in ready_body["unhealthy_dependencies"]
        assert (
            f"timed out after {DEPENDENCY_TIMEOUT}s"
            in (ready_body["dependencies"]["payment_gw"]["error"])
        )

        # /health mirrors /ready
        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        assert health_body["status"] == "unhealthy"
        assert "payment_gw" in health_body["unhealthy_dependencies"]
    finally:
        await hl.stop()


def _assert_health_mirrors_ready(health_body: dict[str, Any], ready_body: dict[str, Any]) -> None:
    """Verify /health payload mirrors /ready payload across all operational fields."""
    assert health_body["status"] == ready_body["status"]
    assert health_body["service"] == ready_body["service"]
    assert health_body["broker_state"] == ready_body["broker_state"]
    assert health_body.keys() == ready_body.keys()
    assert health_body.get("unhealthy_dependencies") == ready_body.get("unhealthy_dependencies")


async def test_a_draining_broker_fails_ready_while_live_stays_200():
    """While `nc.drain()` runs a load balancer must see /ready fail, so no new traffic arrives,
    and the orchestrator must see /live stay 200, so it does not kill the pod mid-drain."""
    svc = CliffracerService(ServiceConfig(name="drain_test_svc", health_port=0))
    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    try:
        _simulate_service_state(svc, running=True, broker_state="draining")

        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "disconnected"
        assert ready_body["broker_state"] == "draining"
        assert ready_body["nats_connected"] is False

        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        _assert_health_mirrors_ready(health_body, ready_body)
    finally:
        await hl.stop()


async def test_health_listener_broker_states_segregation():
    """Verify /live remains 200 during CONNECTING, DISCONNECTED, and CLOSED states while /ready fails."""
    svc = CliffracerService(ServiceConfig(name="broker_test_svc", health_port=0))
    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    try:
        # 1. CONNECTING state
        _simulate_service_state(svc, running=True, broker_state="connecting")
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "connecting"
        assert ready_body["broker_state"] == "connecting"

        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        _assert_health_mirrors_ready(health_body, ready_body)

        # 2. DISCONNECTED state
        _simulate_service_state(svc, running=True, broker_state="disconnected")
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "disconnected"
        assert ready_body["broker_state"] == "disconnected"

        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        _assert_health_mirrors_ready(health_body, ready_body)

        # 3. CLOSED state
        _simulate_service_state(svc, running=True, broker_state="closed")
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        # `status` collapses CLOSED and DISCONNECTED into one word; `broker_state` is the field
        # that tells a reconnecting service from one whose connection is gone for good.
        assert ready_body["status"] == "disconnected"
        assert ready_body["broker_state"] == "closed"

        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        _assert_health_mirrors_ready(health_body, ready_body)
        # /health and /ready are one branch, so mirroring each other proves nothing about the
        # value; the literals above are what it is read against.
        assert health_body["broker_state"] == "closed"
    finally:
        await hl.stop()


async def test_health_listener_broker_flapping_dynamic_recovery():
    """Verify /live remains constant 200 while /ready dynamically toggles 200 <-> 503 as broker flaps."""
    svc = CliffracerService(ServiceConfig(name="flapping_svc", health_port=0))
    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    try:
        # Connected
        _simulate_service_state(svc, running=True, broker_state="connected")
        assert (await _raw_request(hl.port, "/live"))[0] == 200
        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 200
        assert ready_body["broker_state"] == "connected"

        # Disconnected
        _simulate_service_state(svc, running=True, broker_state="disconnected")
        assert (await _raw_request(hl.port, "/live"))[0] == 200
        assert (await _raw_request(hl.port, "/ready"))[0] == 503

        # Re-connected
        _simulate_service_state(svc, running=True, broker_state="connected")
        assert (await _raw_request(hl.port, "/live"))[0] == 200
        assert (await _raw_request(hl.port, "/ready"))[0] == 200
    finally:
        await hl.stop()


@pytest.mark.parametrize("broker_state", ["connected", "none"])
async def test_health_listener_stopped_service_all_probes_503(broker_state):
    """Verify /live, /ready, and /health return 503 when service is stopped.

    With the broker connected, and with no connection at all: "stopped" must win over every
    broker state, so the two are separate cases, not one.
    """
    svc = CliffracerService(ServiceConfig(name="stopped_svc", health_port=0))
    _simulate_service_state(svc, running=False, broker_state=broker_state)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 503
        assert live_body["status"] == "stopped"

        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "stopped"

        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        assert health_body["status"] == "stopped"
    finally:
        await hl.stop()


async def test_health_listener_method_not_allowed_on_all_endpoints():
    """Verify POST, PUT, DELETE, PATCH, and OPTIONS on /live, /ready, /health, /info return 405."""
    svc = CliffracerService(ServiceConfig(name="methods_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    endpoints = ["/live", "/ready", "/health", "/info"]
    disallowed_methods = ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"]

    try:
        for path in endpoints:
            for method in disallowed_methods:
                status, _, body = await _raw_request(
                    hl.port, path, method=method, body="{'some': 'payload'}"
                )
                assert status == 405, f"{method} {path} returned {status}; expected 405"
                assert body == {"error": "method not allowed"}
    finally:
        await hl.stop()


async def test_health_listener_invalid_and_adversarial_paths_return_404():
    """Verify unknown and adversarial paths return 404 Not Found."""
    svc = CliffracerService(ServiceConfig(name="paths_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    invalid_paths = [
        "/",
        "/unknown",
        "/live/",
        "/ready/",
        "/health/",
        "/live/subpath",
        "/ready/subpath",
        "/admin",
        "/metrics",
        "/api/v1/health",
        "/..",
        "/./live",
    ]

    try:
        for path in invalid_paths:
            status, _, body = await _raw_request(hl.port, path)
            assert status == 404, f"GET {path} returned {status}; expected 404"
            assert body == {"error": "not found"}
    finally:
        await hl.stop()


async def test_health_listener_malformed_and_edge_case_requests():
    """Verify HealthListener survives malformed HTTP requests and immediate connection drops."""
    svc = CliffracerService(ServiceConfig(name="malformed_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    try:

        async def still_answers(after: str) -> None:
            status, _, body = await _raw_request(hl.port, "/live")
            assert status == 200, f"/live answered {status} after {after}"
            assert body["status"] == "healthy", after

        # Case 1: Client connects and closes immediately without sending data
        reader, writer = await asyncio.open_connection("127.0.0.1", hl.port)
        writer.close()
        await writer.wait_closed()
        await still_answers("a connection closed without a request")

        # Case 2: Client sends empty lines then closes
        reader, writer = await asyncio.open_connection("127.0.0.1", hl.port)
        writer.write(b"\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await still_answers("empty request lines")

        # Case 3: Client sends binary garbage
        reader, writer = await asyncio.open_connection("127.0.0.1", hl.port)
        writer.write(b"\x00\xff\xfe\xfd\x01\x02\r\n\r\n")
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()
        # Binary garbage has no method "GET": a bad METHOD, answered as one. A 500 here is the
        # handler failing on the garbage and sending the exception text to an unauthenticated caller.
        assert raw.startswith(b"HTTP/1.1 405 "), raw[:80]
        assert b"method not allowed" in raw
        assert b"500" not in raw.split(b"\r\n", 1)[0]

        # Server must still be healthy and answer normal requests cleanly
        status, _, body = await _raw_request(hl.port, "/live")
        assert status == 200
        assert body["status"] == "healthy"
    finally:
        await hl.stop()


# What these three discriminate between, on the same reasoning as the segregation
# test above.
#
# A `/live` blocked behind the slow `/ready` cannot answer until that probe
# finishes, so it would take BLOCKING_PROBE_SLEEP. A `/live` that answers on its
# own path takes the cost of the requests themselves, which is microseconds of
# work per request and a few milliseconds for the burst.
#
# The test makes two assertions with different budgets, and neither dominates.
#
# The ordering one -- the burst must finish while `/ready` is still waiting --
# is budgeted by BLOCKING_PROBE_SLEEP, a constant this file sets. It is a race
# against the probe's remaining sleep rather than a clock-free statement, but
# its budget cannot drift with the host, and it is three times looser than the
# ceiling. That is what makes it the one to read first.
#
# LIVE_BURST_CEILING is budgeted by a measured distribution instead, so it is
# the tighter and the more fragile of the two. It is also the only one that
# catches a `/live` serialised on something OTHER than this probe: a burst
# costing between the ceiling and the sleep finishes before `/ready` does, so
# the ordering assertion is satisfied while every request was in fact blocked.
BLOCKING_PROBE_SLEEP = 1.0
BLOCKING_PROBE_TIMEOUT = 1.5
LIVE_BURST = 20
LIVE_BURST_CEILING = 0.3


async def test_health_listener_non_blocking_concurrency_stress():
    """Stress test: verify /live probe is NEVER blocked or delayed by a slow/hanging /ready probe."""
    svc = CliffracerService(ServiceConfig(name="concurrency_stress_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    slow_started = asyncio.Event()

    async def slow_probe() -> None:
        slow_started.set()
        await asyncio.sleep(BLOCKING_PROBE_SLEEP)
        raise TimeoutError("slow downstream timed out")

    svc.add_dependency("slow_db", slow_probe, timeout=BLOCKING_PROBE_TIMEOUT)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    try:
        # Warm the /live path before it is timed, for the reason the segregation
        # test above warms it: the first request through a path pays for the
        # path, which is not what this measures.
        warm_status, _, _ = await _raw_request(hl.port, "/live")
        assert warm_status == 200

        # Start slow /ready request in background task
        ready_task = asyncio.create_task(_raw_request(hl.port, "/ready"))

        # Wait until the slow probe has definitely begun execution
        await asyncio.wait_for(slow_started.wait(), timeout=BLOCKING_PROBE_TIMEOUT)

        # While /ready is blocked, fire the burst of /live probes
        t0 = time.monotonic()
        live_results = await asyncio.gather(
            *[_raw_request(hl.port, "/live") for _ in range(LIVE_BURST)]
        )
        t_duration = time.monotonic() - t0

        assert len(live_results) == LIVE_BURST
        for status, _, body in live_results:
            assert status == 200
            assert body["status"] == "healthy"

        # The burst finished and /ready has not, so nothing in it waited for the
        # slow probe. This is the assertion that fires when /live is serialised
        # behind THIS probe; a /live serialised behind something else can still
        # satisfy it, which is what the ceiling below is for.
        #
        # A change that serialises /live reds this test and the segregation test
        # above, by different routes. Neither is redundant: that one times a
        # single request against the dependency timeout, this one times a burst
        # against a probe that is deliberately still running.
        assert not ready_task.done(), (
            "the /live burst did not finish until /ready had, so nothing here "
            "shows that /live answers on its own path"
        )

        # The slow /ready task eventually completes with 503
        ready_status, _, ready_body = await ready_task
        assert ready_status == 503
        assert ready_body["status"] == "unhealthy"

        # The duration is judged LAST. `t_duration` was captured above, so
        # nothing is lost by asking here -- and every property that does not
        # need a quiet host has already been asserted, including the ordering
        # one above and /ready's own result.
        #
        # The ordering is not cosmetic: a skip aborts the test, and a refusal
        # placed before `await ready_task` left that task pending and turned a
        # skip into a teardown error. See cliffracer/testing/host_load.py.
        skip_if_the_host_is_too_busy_to_judge(
            f"{LIVE_BURST} concurrent /live probes under a {LIVE_BURST_CEILING}s ceiling"
        )

        # Upper bound. CI p99 0.00904 s (run 4712: eric-7, CPython 3.12.15, n=10, p99 = max); 33x
        # p99.
        assert t_duration < LIVE_BURST_CEILING, (
            f"{LIVE_BURST} /live requests took {t_duration}s, over the "
            f"{LIVE_BURST_CEILING}s ceiling. A /live waiting for the slow probe "
            f"would take about {BLOCKING_PROBE_SLEEP}s, so this is the path "
            "answering slowly rather than the wrong path answering."
        )
    finally:
        await hl.stop()

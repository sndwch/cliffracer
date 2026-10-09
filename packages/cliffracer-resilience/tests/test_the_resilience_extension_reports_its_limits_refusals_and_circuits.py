"""`/health` and `/info` say what the limits have decided, what they are, and each circuit.

The extension reported only which limiter backend was authoritative, so an operator could not see
a limit refusing, what limit a handler had, or an open circuit, short of reading logs.
"""

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import (
    CircuitBreakerConfig,
    InMemoryRateLimiter,
    ResilienceExtension,
    ResilientRpcProxy,
    rate_limit,
)

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit

SECRET = "Bearer s3cret-token-do-not-leak"


def _orders_class() -> type[CliffracerService]:
    """A new class per test: a limiter declared on a class is shared by every service built from it."""

    class Orders(CliffracerService):
        resilience = ResilienceExtension(limiter=InMemoryRateLimiter())
        payments = ResilientRpcProxy(
            "payment_service",
            config=CircuitBreakerConfig(failure_threshold=2, recovery_timeout=60.0),
        )

        @rpc
        @rate_limit(calls=2, window=60.0, key="authorization")
        async def create(self) -> int:
            return 1

        @rpc
        @rate_limit(calls=5, window=30.0)
        async def lookup(self) -> int:
            return 2

        @rpc
        @rate_limit(calls=1, window=10.0, key=lambda ctx: ctx.headers["x-tenant"])
        async def export(self) -> int:
            return 3

    return Orders


async def _started() -> Any:
    svc = _orders_class()(ServiceConfig(name="orders", health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    return svc


async def _call(svc: Any, method: str, headers: dict[str, str]) -> dict:
    msg = AsyncMock()
    msg.subject = f"orders.rpc.{method}"
    msg.data = json.dumps({}).encode()
    msg.headers = headers
    await svc.container._handle_rpc_request(msg)
    return json.loads(msg.respond.await_args_list[0].args[0].decode())


async def test_the_counts_say_how_many_each_handler_let_through_and_refused():
    svc = await _started()
    for _ in range(3):
        await _call(svc, "create", {"Authorization": SECRET})
    for _ in range(4):
        await _call(svc, "lookup", {})

    reported = (await svc.health_check())["resilience"]["rate_limits"]

    assert reported["by_handler"] == {
        "create": {"permitted": 2, "refused": 1},
        "lookup": {"permitted": 4, "refused": 0},
    }
    assert (reported["permitted_total"], reported["refused_total"]) == (6, 1)


async def test_a_payload_validation_refuses_is_counted_as_permitted_by_the_limit_that_let_it_through():
    svc = await _started()
    msg = AsyncMock()
    msg.subject = "orders.rpc.lookup"
    msg.data = json.dumps({"unexpected": 1}).encode()
    msg.headers = {}
    await svc.container._handle_rpc_request(msg)

    reported = svc.resilience.health_details()["rate_limits"]

    assert (reported["permitted_total"], reported["refused_total"]) == (1, 0)


async def test_the_counts_grow_with_handlers_not_with_the_partition_keys_callers_send():
    svc = await _started()
    for caller in range(50):
        await _call(svc, "create", {"Authorization": f"Bearer caller-{caller}"})

    health = svc.resilience.health_details()

    assert list(health["rate_limits"]["by_handler"]) == ["create"]
    assert health["rate_limiter"]["tracked_keys"] == 50
    assert health["rate_limits"]["by_handler"]["create"]["permitted"] == 50


async def test_info_lists_each_limit_by_where_its_key_is_read_never_by_a_value():
    svc = await _started()
    await _call(svc, "create", {"Authorization": SECRET})

    info = svc.get_service_info()["resilience"]

    assert info["rate_limiter"] == "InMemoryRateLimiter"
    assert info["limits"] == {
        "create": {"calls": 2, "window": 60.0, "key": "header:authorization"},
        "lookup": {"calls": 5, "window": 30.0, "key": "handler"},
        "export": {"calls": 1, "window": 10.0, "key": "function"},
    }
    assert SECRET not in json.dumps(info) and SECRET not in json.dumps(await svc.health_check())


async def test_info_names_the_default_limit_when_one_is_set():
    extension = ResilienceExtension(default_calls=7, default_window=3.0)

    assert extension.info_details()["default_limit"] == {"calls": 7, "window": 3.0}
    assert "default_limit" not in ResilienceExtension().info_details()


async def test_a_circuit_is_reported_with_its_state_and_failure_count():
    svc = await _started()

    closed = (await svc.health_check())["resilience"]["circuits"]["payments"]
    assert closed["destination"] == "payment_service"
    assert (closed["state"], closed["failure_count"]) == ("closed", 0)
    assert 0 <= closed["seconds_in_state"] < 5, "a breaker built a moment ago has just entered it"

    breaker = svc.payments.circuit_breaker
    for _ in range(2):
        await breaker.record_failure(ConnectionError("payment_service is down"))

    breaker._last_state_change -= 30.0  # it opened half a minute ago
    opened = (await svc.health_check())["resilience"]["circuits"]["payments"]
    assert (opened["state"], opened["failure_count"]) == ("open", 2)
    assert 30 <= opened["seconds_in_state"] < 35


async def test_a_service_with_no_resilient_proxy_reports_no_circuits():
    class Plain(CliffracerService):
        resilience = ResilienceExtension()

    svc = Plain(ServiceConfig(name="plain", health_port=0))
    await svc.container._setup_extensions()

    assert "circuits" not in svc.resilience.health_details()


async def test_a_proxy_declared_on_a_base_class_is_reported_too():
    class Base(CliffracerService):
        payments = ResilientRpcProxy("payment_service")

    class Derived(Base):
        resilience = ResilienceExtension()

    svc = Derived(ServiceConfig(name="derived", health_port=0))
    await svc.container._setup_extensions()

    assert list(svc.resilience.health_details()["circuits"]) == ["payments"]

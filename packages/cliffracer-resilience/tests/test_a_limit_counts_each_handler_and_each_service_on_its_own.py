"""A limit's counter belongs to the service and the handler that declare it, partitioned by the key's value.

The key a limiter counted under was the key's value alone, or the handler's name when the handler
declared no key. Two handlers keyed on one header therefore shared one timestamp list per caller
(`export` was refused for a call `search` made, and a handler allowed 100 calls spent the budget of
one allowed 1), and two services that name a handler alike and share a limiter's bucket, as every
service does by default, counted in one entry. The replicas of one service share a key, which is what
a distributed limiter is for.
"""

import json
from typing import Any

import pytest
from cliffracer_resilience import (
    InMemoryRateLimiter,
    RateLimiter,
    ResilienceExtension,
    rate_limit,
)
from cliffracer_resilience.rate_limiter import key_fingerprint

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


class Spy(InMemoryRateLimiter):
    """The real limiter, recording every key it is asked to count."""

    def __init__(self) -> None:
        super().__init__()
        self.keys: list[str] = []

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        self.keys.append(key)
        return await super().acquire(key, calls, window)


def _service(name: str, limiter: RateLimiter, *, namespace: str | None = None) -> CliffracerService:
    class Svc(CliffracerService):
        resilience = ResilienceExtension(limiter=limiter)

        @rpc
        @rate_limit(calls=1, window=60.0, key="x-client")
        async def search(self) -> int:
            return 1

        @rpc
        @rate_limit(calls=1, window=60.0, key="x-client")
        async def export(self) -> int:
            return 2

        @rpc
        @rate_limit(calls=100, window=60.0, key="x-client")
        async def browse(self) -> int:
            return 3

        @rpc
        @rate_limit(calls=1, window=60.0)
        async def ping(self) -> int:
            return 4

    return Svc(ServiceConfig(name=name, namespace=namespace, subject_prefix=None, health_port=0))


async def _up(service: CliffracerService) -> CliffracerService:
    await service.container._setup_extensions()
    service._discover_handlers()
    return service


async def _call(
    service: CliffracerService, method: str, client: str | None = "a"
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if client is not None:
        headers["X-Client"] = client
    message = MockMessage(
        service.container._with_namespace(f"{service.config.name}.rpc.{method}"),
        data=b"{}",
        headers=headers,
        reply="_INBOX.r",
    )
    await service.container._handle_rpc_request(message)
    assert message.responded_data is not None
    return json.loads(message.responded_data)


def _admitted(reply: dict[str, Any]) -> bool:
    return reply.get("success") is True


async def test_two_handlers_keyed_on_one_header_each_count_a_caller_on_their_own():
    service = await _up(_service("orders", Spy()))

    search = await _call(service, "search")
    export = await _call(service, "export")
    search_again = await _call(service, "search")

    assert _admitted(search) and _admitted(export), (search, export)
    assert not _admitted(search_again), "search was allowed one call per caller"
    assert search_again["error"] == "refused: rate limit exceeded"


async def test_a_handlers_calls_are_not_spent_by_another_handler_with_a_bigger_limit():
    service = await _up(_service("orders", Spy()))

    browsed = [await _call(service, "browse") for _ in range(5)]
    first_search = await _call(service, "search")

    assert all(_admitted(reply) for reply in browsed)
    assert _admitted(first_search), first_search


async def test_two_services_sharing_a_limiter_count_a_same_named_handler_apart():
    shared = Spy()
    orders = await _up(_service("orders", shared))
    customers = await _up(_service("customers", shared))

    first = await _call(orders, "search")
    other_service = await _call(customers, "search")
    again = await _call(orders, "search")

    assert _admitted(first) and _admitted(other_service), (first, other_service)
    assert not _admitted(again)


async def test_two_namespaces_of_one_service_name_count_apart():
    shared = Spy()
    blue = await _up(_service("orders", shared, namespace="blue"))
    green = await _up(_service("orders", shared, namespace="green"))

    assert _admitted(await _call(blue, "search"))
    assert _admitted(await _call(green, "search"))
    assert not _admitted(await _call(blue, "search"))


async def test_the_replicas_of_one_service_share_a_counter():
    shared = Spy()
    first = await _up(_service("orders", shared))
    second = await _up(_service("orders", shared))

    assert _admitted(await _call(first, "search"))
    assert not _admitted(await _call(second, "search")), "the second replica kept its own count"


async def test_the_key_is_the_service_the_handler_and_the_value():
    spy = Spy()
    service = await _up(_service("orders", spy, namespace="blue"))

    await _call(service, "search", client="a")
    await _call(service, "ping", client=None)

    assert spy.keys == ["blue.orders:search:a", "blue.orders:ping"]


async def test_the_key_of_a_service_with_no_namespace_is_the_service_alone():
    spy = Spy()
    service = await _up(_service("orders", spy))

    await _call(service, "search", client="a")

    assert spy.keys == ["orders:search:a"]


async def test_the_refusal_still_carries_a_fingerprint_of_the_callers_value_alone():
    service = await _up(_service("orders", Spy()))
    await _call(service, "search", client="a")

    refused = await _call(service, "search", client="a")

    assert refused["details"]["key"] == key_fingerprint("a")


async def test_CONTROL_a_different_caller_has_its_own_budget():
    service = await _up(_service("orders", Spy()))

    assert _admitted(await _call(service, "search", client="a"))
    assert _admitted(await _call(service, "search", client="b"))

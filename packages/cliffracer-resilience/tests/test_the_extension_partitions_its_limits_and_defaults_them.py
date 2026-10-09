"""A string key partitions a limit per caller, and a default limit yields to a handler's own.

Both are read through the extension and the wire, with the real in-memory limiter: a key named in
`@rate_limit` must give each value its own budget (a collapse to one bucket named for the key
would admit one client against everyone else's allowance), and `default_calls`/`default_window`
must limit a handler that declares nothing without overriding one that does.
"""

import json

import pytest
from cliffracer_resilience import ResilienceExtension, rate_limit

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


class Limited(CliffracerService):
    resilience = ResilienceExtension()

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="limited", subject_prefix=None))

    @rpc
    @rate_limit(calls=1, window=60.0, key="x-client")
    async def by_header(self) -> int:
        return 1

    @rpc
    @rate_limit(calls=1, window=60.0, key="client_ip", key_source="payload")
    async def by_payload(self, client_ip: str) -> int:
        return 1


class Defaulted(CliffracerService):
    resilience = ResilienceExtension(default_calls=1, default_window=60.0)

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="defaulted", subject_prefix=None))

    @rpc
    async def plain(self) -> int:
        return 1

    @rpc
    @rate_limit(calls=3, window=60.0)
    async def own(self) -> int:
        return 1


async def _started(cls):
    service = cls()
    await service.container._setup_extensions()
    service._discover_handlers()
    return service


async def _call(service, method: str, *, headers: dict | None = None, **payload) -> dict:
    message = MockMessage(
        f"{service.config.name}.rpc.{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        reply="_INBOX.r",
    )
    await service.container._handle_rpc_request(message)
    assert message.responded_data is not None
    return json.loads(message.responded_data)


def _admitted(reply: dict) -> bool:
    return reply.get("success") is True


def _refused_for_rate(reply: dict) -> bool:
    return reply.get("success") is False and reply.get("error") == "refused: rate limit exceeded"


@pytest.mark.asyncio
async def test_a_header_key_gives_each_caller_its_own_budget():
    service = await _started(Limited)

    a1 = await _call(service, "by_header", headers={"X-Client": "a"})
    b1 = await _call(service, "by_header", headers={"X-Client": "b"})
    a2 = await _call(service, "by_header", headers={"X-Client": "a"})

    assert (_admitted(a1), _admitted(b1)) == (True, True), (a1, b1)
    assert _refused_for_rate(a2), a2


@pytest.mark.asyncio
async def test_a_payload_key_gives_each_value_its_own_budget():
    service = await _started(Limited)

    a1 = await _call(service, "by_payload", client_ip="a")
    b1 = await _call(service, "by_payload", client_ip="b")
    a2 = await _call(service, "by_payload", client_ip="a")

    assert (_admitted(a1), _admitted(b1)) == (True, True), (a1, b1)
    assert _refused_for_rate(a2), a2


@pytest.mark.asyncio
async def test_a_default_limit_applies_to_a_handler_that_declares_none():
    service = await _started(Defaulted)

    first = await _call(service, "plain")
    second = await _call(service, "plain")

    assert _admitted(first), first
    assert _refused_for_rate(second), second


@pytest.mark.asyncio
async def test_a_handlers_own_limit_is_not_replaced_by_the_default():
    service = await _started(Defaulted)

    replies = [await _call(service, "own") for _ in range(4)]

    assert [_admitted(r) for r in replies] == [True, True, True, False], replies
    assert _refused_for_rate(replies[3])


@pytest.mark.asyncio
async def test_the_default_and_the_own_limit_are_separate_budgets():
    service = await _started(Defaulted)

    await _call(service, "plain")
    own = [await _call(service, "own") for _ in range(3)]

    assert all(_admitted(r) for r in own), own


ASKED: list[tuple[str, int, float]] = []


class _Recording:
    """A limiter that admits everything and records the budget it was asked about.

    The record is module-level: an extension declared on a class is copied for each service, so
    state on the instance given to the declaration is not the state the service used.
    """

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        ASKED.append((key, calls, window))
        return True

    async def get_retry_after(self, key: str, window: float) -> float:
        return 0.0


@pytest.mark.asyncio
async def test_the_default_hands_the_limiter_the_configured_calls_and_window():
    ASKED.clear()

    class Configured(Defaulted):
        resilience = ResilienceExtension(limiter=_Recording(), default_calls=7, default_window=33.0)

    service = await _started(Configured)

    await _call(service, "plain")

    assert [(calls, window) for _, calls, window in ASKED] == [(7, 33.0)]

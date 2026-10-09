"""A refusal tells the caller exactly when the limit will admit a call again.

A call is admitted once fewer than `calls` permits are live, which is when the permit `calls` from
the newest leaves the window. While a key holds no more permits than its limit that is the oldest
one, and `get_retry_after` (the earliest a permit can free) says the same. A key can hold more after
a limit is lowered, or when replicas race on one bucket, and then `get_retry_after_for`, which a
refusal reads, counts from the right permit. A custom limiter that overrides only `get_retry_after`
is asked that instead. The clocks are the module's own `time`, replaced.
"""

import json
import math
from types import SimpleNamespace

import nats.js.errors
import pytest
from cliffracer_resilience import (
    InMemoryRateLimiter,
    KvRateLimiter,
    RateLimiter,
    RateLimitExceeded,
    ResilienceExtension,
    rate_limit,
)
from cliffracer_resilience import rate_limiter as module

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import ExtensionSetupContext, WorkerContext

pytestmark = pytest.mark.unit

WINDOW = 10.0


class Clock:
    def __init__(self, monkeypatch, at: float) -> None:
        self.at = at
        monkeypatch.setattr(
            module, "time", SimpleNamespace(time=lambda: self.at, monotonic=lambda: self.at)
        )


class Bucket:
    """One key's stamps, as nats-py's KeyValue answers: revisions checked on update."""

    def __init__(self, stamps: list[float]) -> None:
        self.value = json.dumps(stamps).encode()
        self.revision = 1

    async def get(self, key):
        if self.value is None:
            raise nats.js.errors.KeyNotFoundError()
        return SimpleNamespace(value=self.value, revision=self.revision)

    async def create(self, key, value):
        raise nats.js.errors.KeyWrongLastSequenceError()

    async def update(self, key, value, last=None):
        if last != self.revision:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.value, self.revision = value, self.revision + 1


# The issue's case: three live stamps under a limit of two, at 1009 with a 10 s window. Sorted they
# are 1001, 1003, 1006; a call is admitted once 1003 leaves, at 1013, so 4 s.
OVER = [1006.0, 1001.0, 1003.0]


@pytest.mark.parametrize("remembers", [True, False], ids=["remembered", "read-from-bucket"])
async def test_a_kv_hint_over_the_limit_is_when_the_limit_admits(monkeypatch, remembers):
    clock = Clock(monkeypatch, 1009.0)
    limiter = KvRateLimiter(kv=Bucket(OVER), **({} if remembers else {"deny_cache_size": 0}))
    assert await limiter.acquire("k", 2, WINDOW) is False

    hint = await limiter.get_retry_after_for("k", 2, WINDOW)

    assert hint == pytest.approx(4.0)
    clock.at = 1009.0 + hint - 0.01
    assert await limiter.acquire("k", 2, WINDOW) is False
    clock.at = 1009.0 + hint + 0.01
    assert await limiter.acquire("k", 2, WINDOW) is True


@pytest.mark.parametrize("remembers", [True, False], ids=["remembered", "read-from-bucket"])
async def test_CONTROL_a_kv_hint_at_the_limit_is_the_oldest_stamp_either_way(
    monkeypatch, remembers
):
    Clock(monkeypatch, 1009.0)
    limiter = KvRateLimiter(
        kv=Bucket([1006.0, 1003.0]), **({} if remembers else {"deny_cache_size": 0})
    )
    assert await limiter.acquire("k", 2, WINDOW) is False

    exact = await limiter.get_retry_after_for("k", 2, WINDOW)

    assert exact == pytest.approx(4.0) == await limiter.get_retry_after("k", WINDOW)


async def test_an_in_memory_hint_after_the_limit_is_lowered_is_when_the_limit_admits(monkeypatch):
    clock = Clock(monkeypatch, 1001.0)
    limiter = InMemoryRateLimiter()
    for at in (1001.0, 1003.0, 1006.0):  # three permits under a limit of three
        clock.at = at
        assert await limiter.acquire("k", 3, WINDOW) is True
    clock.at = 1009.0
    assert await limiter.acquire("k", 2, WINDOW) is False  # the limit is now two

    hint = await limiter.get_retry_after_for("k", 2, WINDOW)

    assert hint == pytest.approx(4.0)
    assert await limiter.get_retry_after("k", WINDOW) == pytest.approx(2.0)  # the lower bound
    clock.at = 1009.0 + hint - 0.01
    assert await limiter.acquire("k", 2, WINDOW) is False
    clock.at = 1009.0 + hint + 0.01
    assert await limiter.acquire("k", 2, WINDOW) is True


class OnlyTheOldHint(RateLimiter):
    """A custom limiter as the README describes one: it overrides `get_retry_after` alone."""

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        return False

    async def reset(self, key: str | None = None) -> None:
        pass

    async def get_retry_after(self, key: str, window: float) -> float:
        return 7.0


async def test_a_custom_limiter_overriding_only_get_retry_after_is_asked_that():
    assert await OnlyTheOldHint().get_retry_after_for("k", 2, WINDOW) == 7.0


async def test_a_refusal_from_a_custom_limiter_carries_its_hint():
    class Svc(CliffracerService):
        resilience = ResilienceExtension(limiter=OnlyTheOldHint())

        @rpc
        @rate_limit(calls=2, window=WINDOW)
        async def work(self) -> int:
            return 1

    svc = Svc(ServiceConfig(name="svc", health_port=0))
    await svc.resilience.setup(
        ExtensionSetupContext(service_config=svc.config, broker_url="", service=svc)
    )
    ctx = WorkerContext(
        kind="rpc",
        subject="svc.rpc.work",
        headers={},
        correlation_id=None,
        payload={},
        data={"handler_name": "work"},
    )

    with pytest.raises(RateLimitExceeded) as refused:
        await svc.resilience.worker_setup(ctx)

    assert refused.value.retry_after == 7.0


class TellsBoth(OnlyTheOldHint):
    """Answers the lower bound and the exact hint differently, so a caller shows which it asked."""

    async def get_retry_after_for(self, key: str, calls: int, window: float) -> float:
        return 9.0


async def test_the_extensions_refusal_carries_the_exact_hint():
    class Svc(CliffracerService):
        resilience = ResilienceExtension(limiter=TellsBoth())

        @rpc
        @rate_limit(calls=2, window=WINDOW)
        async def work(self) -> int:
            return 1

    svc = Svc(ServiceConfig(name="svc", health_port=0))
    await svc.resilience.setup(
        ExtensionSetupContext(service_config=svc.config, broker_url="", service=svc)
    )
    ctx = WorkerContext(
        kind="rpc",
        subject="svc.rpc.work",
        headers={},
        correlation_id=None,
        payload={},
        data={"handler_name": "work"},
    )

    with pytest.raises(RateLimitExceeded) as refused:
        await svc.resilience.worker_setup(ctx)

    assert refused.value.retry_after == 9.0


async def test_a_handler_called_directly_is_refused_with_the_exact_hint():
    class Svc(CliffracerService):
        @rpc
        @rate_limit(calls=2, window=WINDOW, limiter=TellsBoth())
        async def work(self) -> int:
            return 1

    with pytest.raises(RateLimitExceeded) as refused:
        await Svc(ServiceConfig(name="svc", health_port=0)).work()

    assert refused.value.retry_after == 9.0


# --- a limit of no calls ------------------------------------------------------------------------
#
# A declared limit is at least one call (`rate_limit`, `RateLimitConfig` and the extension's
# default refuse zero where they are built), so only a direct call of a limiter can ask about a
# limit of zero. It never admits a call: the hint is `math.inf`, which a refusal's reply leaves out
# and a JetStream NAK replaces with its configured backoff, as for any hint that is not finite.


async def test_an_in_memory_hint_for_a_limit_of_no_calls_is_never(monkeypatch):
    Clock(monkeypatch, 1009.0)
    limiter = InMemoryRateLimiter()
    assert await limiter.acquire("k", 1, WINDOW) is True
    assert await limiter.acquire("k", 0, WINDOW) is False

    assert await limiter.get_retry_after_for("k", 0, WINDOW) == math.inf


@pytest.mark.parametrize("remembers", [True, False], ids=["remembered", "read-from-bucket"])
async def test_a_kv_limit_of_no_calls_is_refused_and_hints_never(monkeypatch, remembers):
    Clock(monkeypatch, 1009.0)
    limiter = KvRateLimiter(kv=Bucket([1006.0]), **({} if remembers else {"deny_cache_size": 0}))

    assert await limiter.acquire("k", 0, WINDOW) is False
    assert limiter.health_details()["status"] == "distributed"  # a refusal, not a broken bucket
    assert await limiter.get_retry_after_for("k", 0, WINDOW) == math.inf

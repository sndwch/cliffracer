"""How the KV rate limiter prunes, falls back, hints a retry and resets, and how a limit resolves its key.

Prune passes over entries deleted or unreadable since the listing, deletes one exactly a window old,
and reports a failure, raising only without the fallback. Acquire reports a recovered backend only
on a bucket answer, retries a failed open exactly at the interval, counts fallback decisions, admits
a key the moment its stamp is a window old, and writes nothing for a refusal. The retry hint comes
from the fallback while degraded, from memory before the remembered time and from the bucket at it,
and is never negative. Reset keeps other keys' refusals. A keyless limit resolves to the handler,
the subject, then "global", and a synchronous handler behind the extension returns its value.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_resilience import (
    InMemoryRateLimiter,
    KvRateLimiter,
    RateLimitConfig,
    ResilienceExtension,
    rate_limit,
)
from cliffracer_resilience import rate_limiter as module
from cliffracer_resilience.rate_limiter import RateLimiterUnavailableError, RateLimitKeyError

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit


class _Kv:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[bytes, int]] = {}
        self._revision = 0
        self.writes = 0
        self.fail_get: Exception | None = None
        self.fail_keys: Exception | None = None
        self.listed_extra: list[str] = []

    def _next(self) -> int:
        self._revision += 1
        return self._revision

    def put(self, key: str, stamps: list[float] | bytes) -> None:
        value = stamps if isinstance(stamps, bytes) else json.dumps(stamps).encode()
        self.entries[key] = (value, self._next())

    async def get(self, key):
        if self.fail_get is not None:
            raise self.fail_get
        if key not in self.entries:
            raise nats.js.errors.KeyNotFoundError()
        value, revision = self.entries[key]
        return SimpleNamespace(value=value, revision=revision)

    async def create(self, key, value):
        if key in self.entries:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.writes += 1
        self.entries[key] = (value, self._next())

    async def update(self, key, value, last=None):
        if key not in self.entries or (last is not None and self.entries[key][1] != last):
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.writes += 1
        self.entries[key] = (value, self._next())

    async def delete(self, key, last=None):
        if last is not None and key in self.entries and self.entries[key][1] != last:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.entries.pop(key, None)

    async def keys(self):
        if self.fail_keys is not None:
            raise self.fail_keys
        listed = self.listed_extra + list(self.entries)
        if not listed:
            raise nats.js.errors.NoKeysError()
        return listed


class _Js:
    """A JetStream context on a connection that is open until told otherwise."""

    def __init__(self, kv: _Kv | None = None, fail: Exception | None = None) -> None:
        self._nc = SimpleNamespace(is_closed=False)
        self.kv = kv
        self.fail = fail
        self.opens = 0

    async def key_value(self, name):
        self.opens += 1
        if self.fail is not None:
            raise self.fail
        return self.kv


class _Clock:
    def __init__(self) -> None:
        self.wall = 1000.0
        self.mono = 1000.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(module, "time", fake)  # this module's clock only, not the event loop's
    return fake


async def _make_unavailable(limiter: KvRateLimiter, kv: _Kv) -> None:
    kv.fail_get = RuntimeError("the store is down")
    with pytest.raises(RateLimiterUnavailableError):
        await limiter.acquire("broken", 1, 60.0)
    kv.fail_get = None
    assert limiter.health_details()["status"] == "unavailable"


# ---------------------------------------------------------------- prune_expired


async def test_prune_passes_over_an_entry_deleted_after_the_bucket_was_listed():
    kv = _Kv()
    kv.listed_extra = ["sha256_gone"]
    limiter = KvRateLimiter(kv=kv)

    assert await limiter.prune_expired(60.0) == 0
    assert limiter.health_details()["status"] == "distributed"


async def test_prune_keeps_an_entry_it_cannot_read_and_does_not_fail():
    kv = _Kv()
    kv.put("sha256_corrupt", b"not json")
    limiter = KvRateLimiter(kv=kv)

    assert await limiter.prune_expired(60.0) == 0
    assert "sha256_corrupt" in kv.entries


async def test_prune_deletes_an_entry_whose_newest_stamp_is_exactly_a_window_old(clock):
    kv = _Kv()
    kv.put("sha256_old", [1000.0])
    limiter = KvRateLimiter(kv=kv)
    clock.wall = 1010.0

    assert await limiter.prune_expired(10.0) == 1
    assert kv.entries == {}


async def test_a_failed_prune_is_reported_and_raises_only_without_the_fallback():
    kv = _Kv()
    kv.fail_keys = RuntimeError("the store is down")

    strict = KvRateLimiter(kv=kv)
    with pytest.raises(RateLimiterUnavailableError):
        await strict.prune_expired(60.0)
    assert strict.health_details()["status"] == "unavailable"
    assert strict.health_details()["last_error_type"] == "RuntimeError"

    lenient = KvRateLimiter(kv=kv, in_memory_fallback=True)
    assert await lenient.prune_expired(60.0) == 0
    assert lenient.health_details()["status"] == "degraded"


# ---------------------------------------------------------------- acquire


async def test_a_refusal_answered_from_memory_does_not_report_the_backend_recovered():
    kv = _Kv()
    js = _Js(kv)
    limiter = KvRateLimiter(in_memory_fallback=True)
    await limiter.init_kv(js=js)  # as the extension opens it
    assert await limiter.acquire("a", 1, 60.0) is True
    assert await limiter.acquire("a", 1, 60.0) is False  # remembered
    kv.fail_get = RuntimeError("the store is down")
    await limiter.acquire("b", 1, 60.0)
    kv.fail_get = None
    assert limiter.health_details()["status"] == "degraded"

    assert await limiter.acquire("a", 1, 60.0) is False
    assert limiter.health_details()["status"] == "degraded"

    js._nc.is_closed = True  # the connection the bucket was opened on, and no other
    assert await limiter.acquire("a", 1, 60.0) is False
    assert limiter.health_details()["status"] == "degraded"


async def test_a_failed_open_is_tried_again_exactly_when_the_interval_has_passed(clock):
    js = _Js(fail=RuntimeError("no responders"))
    limiter = KvRateLimiter(js=js, in_memory_fallback=True)
    await limiter.acquire("caller", 100, 60.0)
    assert js.opens == 1

    clock.mono = 1000.0 + module.BUCKET_REOPEN_SECONDS
    await limiter.acquire("caller", 100, 60.0)
    assert js.opens == 2


async def test_a_limiter_with_no_bucket_reports_degraded_and_counts_every_fallback_decision():
    limiter = KvRateLimiter(in_memory_fallback=True)

    assert await limiter.acquire("a", 100, 60.0) is True
    assert limiter.health_details()["status"] == "degraded"
    assert limiter.health_details()["last_error_type"] == "RuntimeError"
    assert await limiter.acquire("a", 100, 60.0) is True
    assert await limiter.acquire("a", 100, 60.0) is True
    assert limiter.health_details()["fallback_total"] == 3


async def test_a_key_is_admitted_again_the_moment_its_stamp_is_a_whole_window_old(clock):
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv)
    assert await limiter.acquire("a", 1, 10.0) is True
    assert await limiter.acquire("a", 1, 10.0) is False

    clock.wall = 1010.0
    assert await limiter.acquire("a", 1, 10.0) is True
    # and a limiter that never refused the key reads the same boundary from the bucket
    kv2 = _Kv()
    clock.wall = 1000.0
    fresh = KvRateLimiter(kv=kv2)
    assert await fresh.acquire("b", 1, 10.0) is True
    clock.wall = 1010.0
    assert await KvRateLimiter(kv=kv2).acquire("b", 1, 10.0) is True


async def test_a_limit_of_zero_calls_refuses_and_writes_nothing():
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv)

    assert await limiter.acquire("a", 0, 60.0) is False
    assert kv.entries == {}


async def test_a_refusal_with_nothing_expired_writes_nothing_to_the_bucket():
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv, deny_cache_size=0)
    assert await limiter.acquire("a", 1, 60.0) is True
    writes = kv.writes

    assert await limiter.acquire("a", 1, 60.0) is False
    assert await limiter.acquire("a", 1, 60.0) is False
    assert kv.writes == writes


async def _create(limiter, kv):
    return await limiter.acquire("new", 1, 60.0)


async def _update(limiter, kv):
    return await limiter.acquire("twice", 2, 60.0)


async def _refuse(limiter, kv):
    return await limiter.acquire("once", 1, 60.0)


async def _zero(limiter, kv):
    return await limiter.acquire("zero", 0, 60.0)


async def _hint_missing(limiter, kv):
    return await limiter.get_retry_after("missing", 60.0)


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (_create, True),
        (_update, True),
        (_refuse, False),
        (_zero, False),
        (_hint_missing, 0.0),
    ],
    ids=["create", "update", "refuse", "zero-calls", "hint-missing-key"],
)
async def test_a_bucket_answer_after_a_failure_reports_the_backend_recovered(operation, expected):
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv)
    assert await limiter.acquire("twice", 2, 60.0) is True
    assert await limiter.acquire("once", 1, 60.0) is True
    await _make_unavailable(limiter, kv)

    assert await operation(limiter, kv) == expected
    assert limiter.health_details()["status"] == "distributed"
    assert limiter.health_details()["last_error_type"] is None


async def test_a_hint_read_from_the_bucket_after_a_failure_reports_the_backend_recovered():
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv)
    assert await limiter.acquire("once", 1, 60.0) is True
    await _make_unavailable(limiter, kv)

    assert 59.0 < await limiter.get_retry_after("once", 60.0) <= 60.0
    assert limiter.health_details()["status"] == "distributed"


# ---------------------------------------------------------------- get_retry_after


async def test_while_degraded_the_retry_hint_comes_from_the_counts_that_refused():
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv, in_memory_fallback=True)
    kv.fail_get = RuntimeError("the store is down")
    assert await limiter.acquire("k", 1, 60.0) is True
    assert await limiter.acquire("k", 1, 60.0) is False
    kv.fail_get = None  # the store answers again, and holds nothing for "k"

    hint = await limiter.get_retry_after("k", 60.0)
    assert isinstance(hint, float)
    assert 59.0 < hint <= 60.0


async def test_a_limiter_with_no_bucket_and_no_fallback_refuses_to_estimate():
    with pytest.raises(RateLimiterUnavailableError):
        await KvRateLimiter().get_retry_after("k", 60.0)


async def test_the_retry_hint_is_never_negative_once_the_remembered_time_has_passed(clock):
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv)
    assert await limiter.acquire("a", 1, 10.0) is True
    assert await limiter.acquire("a", 1, 10.0) is False

    clock.wall = 1011.0
    assert await limiter.get_retry_after("a", 10.0) == 0.0


async def test_at_the_remembered_retry_time_the_hint_is_read_from_the_bucket(clock):
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv)
    assert await limiter.acquire("a", 2, 10.0) is True
    clock.wall = 1001.0
    assert await limiter.acquire("a", 2, 10.0) is True
    assert await limiter.acquire("a", 2, 10.0) is False  # retry hint remembered as 1010

    clock.wall = 1010.0
    assert await limiter.get_retry_after("a", 10.0) == 1.0


async def test_before_the_remembered_retry_time_the_hint_is_answered_from_memory(clock):
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv)
    assert await limiter.acquire("a", 1, 10.0) is True
    assert await limiter.acquire("a", 1, 10.0) is False
    kv.entries.clear()  # another replica reset the key: seen only when the moment passes

    clock.wall = 1009.5
    assert await limiter.get_retry_after("a", 10.0) == 0.5


async def test_the_retry_hint_for_a_key_never_seen_is_zero():
    assert await KvRateLimiter(kv=_Kv()).get_retry_after("never", 60.0) == 0.0


@pytest.mark.parametrize("failure", ["read", "corrupt"])
async def test_a_hint_the_bucket_cannot_give_raises_or_falls_back_and_is_reported(failure):
    def broken() -> _Kv:
        kv = _Kv()
        if failure == "read":
            kv.fail_get = RuntimeError("the store is down")
        else:
            kv.put(KvRateLimiter()._safe_key("k"), b"not json")
        return kv

    strict = KvRateLimiter(kv=broken())
    with pytest.raises(RateLimiterUnavailableError):
        await strict.get_retry_after("k", 60.0)
    assert strict.health_details()["status"] == "unavailable"

    lenient = KvRateLimiter(kv=broken(), in_memory_fallback=True)
    assert await lenient.get_retry_after("k", 60.0) == 0.0
    assert lenient.health_details()["status"] == "degraded"


async def test_the_retry_hint_counts_from_the_oldest_stamp_still_inside_the_window(clock):
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv)
    kv.put(limiter._safe_key("a"), [1000.0, 1005.0])

    clock.wall = 1010.0
    assert await limiter.get_retry_after("a", 10.0) == 5.0


async def test_the_retry_hint_for_a_key_whose_stamps_all_expired_is_zero(clock):
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv)
    kv.put(limiter._safe_key("a"), [1000.0])

    clock.wall = 1100.0
    assert await limiter.get_retry_after("a", 10.0) == 0.0


# ---------------------------------------------------------------- reset


async def test_resetting_one_key_keeps_the_other_keys_remembered_refusals():
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv)
    for key in ("a", "b"):
        assert await limiter.acquire(key, 1, 60.0) is True
        assert await limiter.acquire(key, 1, 60.0) is False

    await limiter.reset("a")
    kv.entries.pop(limiter._safe_key("b"))  # another replica reset "b"

    assert await limiter.acquire("b", 1, 60.0) is False
    assert await limiter.acquire("a", 1, 60.0) is True


async def test_resetting_a_limiter_with_no_bucket_is_not_an_error():
    limiter = KvRateLimiter()
    await limiter.reset()
    await limiter.reset("a")


async def test_reset_clears_the_fallback_counts():
    limiter = KvRateLimiter(in_memory_fallback=True)
    assert await limiter.acquire("k", 1, 60.0) is True
    assert await limiter.acquire("k", 1, 60.0) is False

    await limiter.reset("k")
    assert await limiter.acquire("k", 1, 60.0) is True


# ---------------------------------------------------------------- RateLimitConfig.resolve_key


def _ctx(subject, data) -> WorkerContext:
    return WorkerContext(
        kind="rpc", subject=subject, headers={}, correlation_id=None, payload={}, data=data
    )


def test_a_keyless_limit_resolves_to_the_handler_then_the_subject_then_global():
    config = RateLimitConfig(calls=1, window=1.0)

    assert config.resolve_key(_ctx("svc.rpc.h", {"handler_name": "h"})) == "h"
    assert config.resolve_key(_ctx("svc.rpc.h", {})) == "svc.rpc.h"
    assert config.resolve_key(_ctx(None, {})) == "global"
    assert config.resolve_key() == "global"


def test_a_direct_call_partitions_a_payload_key_by_its_argument():
    config = RateLimitConfig(calls=1, window=1.0, key="user", key_source="payload")

    assert config.resolve_key(None, user="ann") == "ann"
    assert config.resolve_key(None, user=7) == "7"
    with pytest.raises(RateLimitKeyError):
        config.resolve_key(None, other="x")


def test_a_direct_call_does_not_read_a_header_key_from_its_arguments():
    config = RateLimitConfig(calls=1, window=1.0, key="user")

    with pytest.raises(RateLimitKeyError):
        config.resolve_key(None, user="ann")
    with pytest.raises(RateLimitKeyError):
        config.resolve_key(None)


# ---------------------------------------------------------------- the wrapper behind the extension


async def test_a_synchronous_handler_behind_the_extension_returns_its_value():
    class Echo(CliffracerService):
        resilience = ResilienceExtension(limiter=InMemoryRateLimiter())

        @rpc
        @rate_limit(calls=5, window=60.0)
        def echo(self, text: str) -> str:
            return f"echo {text}"

    svc = Echo(ServiceConfig(name="echo", health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject = "echo.rpc.echo"
    msg.data = json.dumps({"text": "hi"}).encode()
    msg.headers = None
    await svc.container._handle_rpc_request(msg)
    reply = json.loads(msg.respond.await_args_list[0].args[0].decode())

    assert reply.get("result") == "echo hi", reply

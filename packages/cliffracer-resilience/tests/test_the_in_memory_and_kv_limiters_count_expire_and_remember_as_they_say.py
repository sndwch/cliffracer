"""How the in-memory and KV rate limiters count, expire, report and remember refusals.

The base limiter prunes nothing and hints no wait; a key's fingerprint is `sha256:` and twelve hex
digits. The in-memory limiter sweeps only past `max_keys`, counts a permit until exactly one window
old, resets one key or all, and takes its retry hint from the oldest live permit. The KV limiter
reports its bucket's health, opens and reopens it on the connection it was given or an older open
one, treats stored state that is not a list of finite timestamps as invalid, gives up after five
conflicting writes, and bounds its remembered refusals, dropping expired ones first.
"""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import nats.js.errors
import pytest
from cliffracer_resilience import InMemoryRateLimiter, KvRateLimiter
from cliffracer_resilience import rate_limiter as rl
from cliffracer_resilience.rate_limiter import (
    RateLimiter,
    RateLimiterUnavailableError,
    key_fingerprint,
)

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(rl, "time", c)
    return c


def _sk(key: str) -> str:
    return "sha256_" + hashlib.sha256(key.encode()).hexdigest()


class _Kv:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[bytes, int]] = {}
        self.rev = 0
        self.gets: list[str] = []
        self.gates: dict[str, list[asyncio.Event]] = {}
        self.conflict_updates = False
        self.fail_keys: Exception | None = None

    def _next(self) -> int:
        self.rev += 1
        return self.rev

    def put(self, key: str, stamps) -> None:
        raw = stamps if isinstance(stamps, bytes) else json.dumps(stamps).encode()
        self.entries[_sk(key)] = (raw, self._next())

    async def get(self, key):
        self.gets.append(key)
        gate = self.gates.get(key)
        if gate:
            await gate.pop(0).wait()
        if key not in self.entries:
            raise nats.js.errors.KeyNotFoundError()
        value, revision = self.entries[key]
        return SimpleNamespace(value=value, revision=revision)

    async def create(self, key, value):
        if key in self.entries:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.entries[key] = (value, self._next())

    async def update(self, key, value, last=None):
        if self.conflict_updates:
            raise nats.js.errors.KeyWrongLastSequenceError()
        if key not in self.entries or (last is not None and self.entries[key][1] != last):
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.entries[key] = (value, self._next())

    async def delete(self, key, last=None):
        self.entries.pop(key, None)

    async def keys(self):
        if self.fail_keys is not None:
            raise self.fail_keys
        if not self.entries:
            raise nats.js.errors.NoKeysError()
        return list(self.entries)


class _Js:
    """A JetStream context stand-in whose connection can be closed."""

    def __init__(self, kv=None, fail: Exception | None = None, with_nc: bool = True) -> None:
        self.kv = kv if kv is not None else _Kv()
        self.fail = fail
        self.opens = 0
        if with_nc:
            self._nc = SimpleNamespace(is_closed=False)

    def close(self) -> None:
        self._nc.is_closed = True

    async def key_value(self, name):
        self.opens += 1
        if self.fail is not None:
            raise self.fail
        return self.kv


class _SlotJs:
    """A stand-in that cannot be weakly referenced."""

    __slots__ = ("_nc", "kv", "opens")

    def __init__(self) -> None:
        self._nc = SimpleNamespace(is_closed=False)
        self.kv = _Kv()
        self.opens = 0

    async def key_value(self, name):
        self.opens += 1
        return self.kv


# --- the base class ---------------------------------------------------------------------------


class _Bare(RateLimiter):
    async def acquire(self, key, calls, window):
        return True

    async def reset(self, key=None):
        return None


async def test_a_limiter_without_its_own_prune_or_hint_prunes_nothing_and_hints_no_wait():
    bare = _Bare()
    pruned = await bare.prune_expired(60.0)
    hint = await bare.get_retry_after("k", 60.0)
    assert pruned == 0 and isinstance(pruned, int)
    assert hint == 0.0 and isinstance(hint, float)


def test_a_key_fingerprint_is_the_first_twelve_hex_digits_of_its_sha256():
    digest = hashlib.sha256(b"secret-key").hexdigest()
    assert key_fingerprint("secret-key") == f"sha256:{digest[:12]}"


# --- in-memory limiter --------------------------------------------------------------------------


async def test_the_in_memory_table_is_swept_only_once_it_exceeds_max_keys(clock):
    limiter = InMemoryRateLimiter(max_keys=2)
    assert await limiter.acquire("a", 1, 1.0)
    assert await limiter.acquire("b", 1, 1.0)
    clock.t += 10
    assert await limiter.acquire("c", 1, 1.0)
    assert limiter.tracked_keys == 3, "a table AT max_keys was swept"
    clock.t += 10
    assert await limiter.acquire("d", 1, 1.0)
    assert limiter.tracked_keys == 1, "a table past max_keys was not swept"


async def test_the_default_in_memory_limiter_sweeps_once_it_holds_more_than_ten_thousand_keys(
    clock,
):
    limiter = InMemoryRateLimiter()
    for i in range(10_001):
        await limiter.acquire(f"k{i}", 1, 1.0)
    clock.t += 10
    assert await limiter.acquire("new", 1, 1.0)
    assert limiter.tracked_keys == 1


async def test_a_permit_exactly_one_window_old_no_longer_counts(clock):
    limiter = InMemoryRateLimiter()
    assert await limiter.acquire("k", 1, 60.0)
    clock.t += 60
    assert await limiter.acquire("k", 1, 60.0)


async def test_a_refusal_with_no_permits_to_count_leaves_no_entry():
    limiter = InMemoryRateLimiter()
    assert await limiter.acquire("k", 0, 60.0) is False
    assert limiter.tracked_keys == 0


async def test_in_memory_reset_of_one_key_keeps_the_others_and_reset_of_none_clears_all():
    limiter = InMemoryRateLimiter()
    assert await limiter.acquire("a", 1, 60.0)
    assert await limiter.acquire("b", 1, 60.0)
    await limiter.reset("a")
    assert limiter.tracked_keys == 1
    assert await limiter.acquire("b", 1, 60.0) is False
    assert await limiter.acquire("a", 1, 60.0) is True
    await limiter.reset()
    assert limiter.tracked_keys == 0
    assert await limiter.acquire("b", 1, 60.0) is True


async def test_the_in_memory_retry_hint_is_when_the_oldest_live_permit_leaves(clock):
    limiter = InMemoryRateLimiter()
    t0 = clock.t
    assert await limiter.acquire("k", 2, 60.0)
    clock.t = t0 + 30
    assert await limiter.acquire("k", 2, 60.0)
    clock.t = t0 + 60  # the first permit is exactly one window old: it has left
    assert await limiter.get_retry_after("k", 60.0) == 30.0


async def test_a_retry_hint_for_a_key_whose_window_has_passed_drops_the_key(clock):
    limiter = InMemoryRateLimiter()
    assert await limiter.acquire("k", 1, 60.0)
    clock.t += 100
    assert await limiter.get_retry_after("k", 60.0) == 0.0
    assert limiter.tracked_keys == 0


async def test_in_memory_prune_reads_expiry_against_the_given_window(clock):
    limiter = InMemoryRateLimiter()
    t0 = clock.t
    assert await limiter.acquire("k", 1, 60.0)
    clock.t = t0 + 10
    assert await limiter.prune_expired(60.0) == 0
    assert limiter.tracked_keys == 1
    clock.t = t0 + 60
    assert await limiter.prune_expired(60.0) == 1
    assert limiter.tracked_keys == 0


# --- kv limiter: health and opening ---------------------------------------------------------------


async def test_a_fresh_kv_limiter_reports_itself_uninitialized():
    assert KvRateLimiter().health_details() == {
        "backend": "nats-kv",
        "status": "uninitialized",
        "fallback_enabled": False,
        "fallback_total": 0,
        "last_error_type": None,
    }


async def test_a_kv_limiter_given_a_bucket_reports_it_distributed():
    assert KvRateLimiter(kv=_Kv()).health_details()["status"] == "distributed"


async def test_a_kv_limiter_with_no_bucket_refuses_by_name_or_falls_back():
    limiter = KvRateLimiter()
    with pytest.raises(RateLimiterUnavailableError):
        await limiter.acquire("k", 1, 60.0)
    assert limiter.health_details()["last_error_type"] == "RuntimeError"
    assert await KvRateLimiter(in_memory_fallback=True).acquire("k", 1, 60.0) is True


async def test_init_kv_with_nothing_to_open_changes_nothing():
    limiter = KvRateLimiter()
    await limiter.init_kv()
    assert limiter.health_details()["status"] == "uninitialized"
    assert limiter.health_details()["last_error_type"] is None


async def test_a_bucket_handed_to_init_kv_is_used_and_reported_distributed():
    limiter = KvRateLimiter()
    kv = _Kv()
    await limiter.init_kv(kv=kv)
    assert limiter.health_details()["status"] == "distributed"
    assert await limiter.acquire("k", 1, 60.0) is True
    assert _sk("k") in kv.entries


async def test_a_bucket_opened_from_jetstream_is_reported_distributed():
    limiter = KvRateLimiter(js=_Js())
    assert limiter.health_details()["status"] == "uninitialized"
    await limiter.init_kv()
    assert limiter.health_details()["status"] == "distributed"


async def test_init_kv_on_a_limiter_that_holds_a_bucket_reports_it_healthy_again():
    kv = _Kv()
    limiter = KvRateLimiter(kv=kv, in_memory_fallback=True)
    kv.fail_keys = RuntimeError("store down")
    await limiter.reset()
    assert limiter.health_details()["status"] == "degraded"
    await limiter.init_kv()
    assert limiter.health_details()["status"] == "distributed"
    assert limiter.health_details()["last_error_type"] is None


async def test_a_failed_bucket_open_is_reported_with_its_error():
    degraded = KvRateLimiter(js=_Js(fail=RuntimeError("down")), in_memory_fallback=True)
    await degraded.init_kv()
    assert degraded.health_details()["status"] == "degraded"
    assert degraded.health_details()["last_error_type"] == "RuntimeError"

    strict = KvRateLimiter(js=_Js(fail=RuntimeError("down")))
    with pytest.raises(RateLimiterUnavailableError):
        await strict.init_kv()
    assert strict.health_details()["status"] == "unavailable"
    assert strict.health_details()["last_error_type"] == "RuntimeError"


async def test_a_failed_reopen_on_a_new_connection_waits_out_the_reopen_interval():
    first = _Js()
    limiter = KvRateLimiter(in_memory_fallback=True)
    await limiter.init_kv(js=first)
    first.close()
    second = _Js(fail=RuntimeError("down"))
    await limiter.init_kv(js=second)
    assert second.opens == 1
    await limiter.acquire("k", 5, 60.0)
    await limiter.acquire("k", 5, 60.0)
    assert second.opens == 1, "each dispatch retried the open inside the reopen interval"


async def test_a_stand_in_without_a_connection_counts_as_open():
    bare = _Js(with_nc=False)
    other = _Js()
    limiter = KvRateLimiter()
    await limiter.init_kv(js=bare)
    await limiter.init_kv(js=other)
    assert await limiter.acquire("k", 1, 60.0)
    assert _sk("k") in bare.kv.entries
    assert other.kv.entries == {}


async def test_a_limiter_moves_to_an_older_open_connection_when_the_newest_has_closed():
    d, e, f = _Js(), _Js(), _Js()
    limiter = KvRateLimiter()
    for js in (d, e, f):
        await limiter.init_kv(js=js)
    d.close()
    f.close()
    assert await limiter.acquire("k", 1, 60.0)
    assert _sk("k") in e.kv.entries
    assert d.kv.entries == {} and f.kv.entries == {}


async def test_a_connection_that_cannot_be_weakly_referenced_is_still_moved_to():
    first = _Js()
    second = _SlotJs()
    limiter = KvRateLimiter()
    await limiter.init_kv(js=first)
    await limiter.init_kv(js=second)
    first.close()
    assert await limiter.acquire("k", 1, 60.0)
    assert _sk("k") in second.kv.entries
    assert first.kv.entries == {}


# --- kv limiter: stored state ---------------------------------------------------------------------


async def test_prune_skips_an_entry_that_is_not_a_timestamp_list(clock):
    kv = _Kv()
    kv.put("odd", b"5")
    kv.put("old", [clock.t - 100])
    limiter = KvRateLimiter(kv=kv)
    assert await limiter.prune_expired(60.0) == 1
    assert _sk("odd") in kv.entries and _sk("old") not in kv.entries


async def test_a_non_finite_timestamp_makes_the_stored_state_invalid():
    kv = _Kv()
    kv.put("k", b"[Infinity]")
    with pytest.raises(RateLimiterUnavailableError):
        await KvRateLimiter(kv=kv).acquire("k", 1, 60.0)


async def test_a_zero_call_limit_on_a_key_whose_stamps_have_all_expired_is_a_plain_refusal(clock):
    kv = _Kv()
    kv.put("k", [clock.t - 100])
    assert await KvRateLimiter(kv=kv).acquire("k", 0, 60.0) is False


async def test_a_kv_limiter_gives_up_after_five_conflicting_writes(clock):
    kv = _Kv()
    kv.put("k", [clock.t - 1])
    kv.conflict_updates = True
    with pytest.raises(RateLimiterUnavailableError):
        await KvRateLimiter(kv=kv).acquire("k", 10, 60.0)
    assert len(kv.gets) == 5


# --- kv limiter: remembered refusals ---------------------------------------------------------------


async def test_a_deny_cache_of_one_remembers_one_refusal(clock):
    kv = _Kv()
    kv.put("k", [clock.t - 1])
    limiter = KvRateLimiter(kv=kv, deny_cache_size=1)
    assert await limiter.acquire("k", 1, 60.0) is False
    assert await limiter.acquire("k", 1, 60.0) is False
    assert limiter.denied_locally == 1
    assert len(kv.gets) == 1


async def test_the_default_deny_cache_remembers_ten_thousand_refusals(clock):
    kv = _Kv()
    keys = [f"k{i}" for i in range(10_001)]
    for key in keys:
        kv.put(key, [clock.t - 1])
    limiter = KvRateLimiter(kv=kv)
    for key in keys:
        assert await limiter.acquire(key, 1, 60.0) is False
    kv.gets.clear()
    assert await limiter.acquire(keys[1], 1, 60.0) is False
    assert kv.gets == [], "the second-oldest refusal was not remembered"
    assert await limiter.acquire(keys[0], 1, 60.0) is False
    assert kv.gets == [_sk(keys[0])], "the oldest refusal was still remembered past the bound"


async def test_the_most_recently_refused_key_survives_the_deny_cache_bound(clock):
    kv = _Kv()
    for key in "ABC":
        kv.put(key, [clock.t - 1])
    limiter = KvRateLimiter(kv=kv, deny_cache_size=2)
    first, second = asyncio.Event(), asyncio.Event()
    kv.gates[_sk("A")] = [first, second]
    t1 = asyncio.create_task(limiter.acquire("A", 1, 60.0))
    t2 = asyncio.create_task(limiter.acquire("A", 1, 60.0))
    await asyncio.sleep(0)
    first.set()
    assert await t1 is False
    assert await limiter.acquire("B", 1, 60.0) is False
    second.set()
    assert await t2 is False  # A refused again, after B: A is now the newest
    assert await limiter.acquire("C", 1, 60.0) is False  # the bound drops the oldest: B
    kv.gets.clear()
    assert await limiter.get_retry_after("A", 60.0) == 59.0
    assert kv.gets == [], "A's retry hint was dropped as if it were the oldest"
    assert await limiter.acquire("A", 1, 60.0) is False
    assert kv.gets == [], "A's refusal was dropped as if it were the oldest"


async def test_a_remembered_refusal_whose_moment_has_come_is_dropped_before_a_live_one(clock):
    t0 = clock.t
    kv = _Kv()
    for key in ("Y", "X", "Z"):
        kv.put(key, [t0])
    limiter = KvRateLimiter(kv=kv, deny_cache_size=2)
    assert await limiter.acquire("Y", 1, 100.0) is False  # remembered until t0 + 100
    assert await limiter.acquire("X", 1, 10.0) is False  # remembered until t0 + 10
    clock.t = t0 + 10
    assert await limiter.acquire("Z", 1, 100.0) is False
    kv.gets.clear()
    assert await limiter.acquire("Y", 1, 100.0) is False
    assert kv.gets == [], "a live refusal was dropped while one whose moment had come was kept"


async def test_reset_of_one_key_forgets_its_refusals_and_keeps_the_others(clock):
    kv = _Kv()
    kv.put("A", [clock.t - 1])
    kv.put("B", [clock.t - 1])
    limiter = KvRateLimiter(kv=kv)
    assert await limiter.acquire("A", 1, 60.0) is False
    assert await limiter.acquire("B", 1, 60.0) is False
    await limiter.reset("A")
    kv.gets.clear()
    assert await limiter.acquire("B", 1, 60.0) is False
    assert await limiter.get_retry_after("B", 60.0) == 59.0
    assert kv.gets == [], "reset('A') forgot B's remembered refusal"
    assert await limiter.get_retry_after("A", 60.0) == 0.0


async def test_reset_of_every_key_forgets_every_retry_hint(clock):
    kv = _Kv()
    kv.put("A", [clock.t - 1])
    limiter = KvRateLimiter(kv=kv)
    assert await limiter.acquire("A", 1, 60.0) is False
    await limiter.reset()
    assert await limiter.get_retry_after("A", 60.0) == 0.0

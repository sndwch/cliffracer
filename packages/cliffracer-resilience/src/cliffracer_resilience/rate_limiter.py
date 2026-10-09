"""Sliding window rate limiting and call rejection.

Provides sliding window rate limiting backends (in-memory and NATS KV distributed),
the @rate_limit decorator, and the RateLimitExceeded rejection exception.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import math
import random
import time
import weakref
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

import nats.js.errors
from loguru import logger

from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.extension import RetryMessage

if TYPE_CHECKING:
    from cliffracer.core.extension import WorkerContext

_rate_limit_checked: ContextVar[frozenset[int]] = ContextVar(
    "_rate_limit_checked", default=frozenset()
)


class RateLimitKeyError(ValueError):
    """A declared partition key was absent from its authoritative source."""


class RateLimiterUnavailableError(RuntimeError):
    """The distributed limiter could not make an authoritative decision."""


class _RateLimitStateError(ValueError):
    """Persisted limiter state is not a list of finite timestamps."""


class _CasExhaustedError(RuntimeError):
    """The distributed limiter could not win a bounded CAS retry loop."""


def _key_digest(key: str) -> str:
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def key_fingerprint(key: str) -> str:
    """Return a safe diagnostic identity for a potentially secret partition key."""
    return f"sha256:{_key_digest(key)[:12]}"


class RateLimitExceeded(RetryMessage):
    """Raised when a request exceeds the configured rate limit.

    Inherits from RetryMessage so the worker loop skips the handler and
    translates RPC calls to a wire error:
        {"error": "refused: rate limit exceeded", ...}
    while durable events are NAKed until the permit can be acquired.
    """

    def __init__(
        self,
        reason: str = "rate limit exceeded",
        details: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(reason, retry_after=retry_after)
        self.details = details or {}


class RateLimiter(ABC):
    """Abstract base class for sliding window rate limiters.

    A limiter counts permits, not messages. `ResilienceExtension` calls `acquire` once for every
    dispatch it sees and has no record of which message that is, so a JetStream redelivery of a
    message spends another permit, and a permit spent on a message that a later extension then
    refuses is not given back: the limit measures delivery attempts. A message the limit itself
    refuses spends none, and is redelivered later as a first attempt.
    """

    def health_details(self) -> dict[str, Any] | None:
        """What `/health` reports for this limiter: its backend and whether it is authoritative.

        A limiter that is distributed, or can degrade, says so here. `None`, the default, has
        `/health` name the class and report the status `unreported`, which claims nothing about it.
        """
        return None

    @abstractmethod
    async def acquire(self, key: str, calls: int, window: float) -> bool:
        """Attempt to acquire a call permit under the sliding window.

        Args:
            key: Rate limit partition key
            calls: Maximum allowed calls within the window
            window: Time window in seconds

        Returns:
            True if permitted, False if limit exceeded. A permit is spent by every call that
            returns True, including for a message delivered before, and is never returned.
        """
        ...

    @abstractmethod
    async def reset(self, key: str | None = None) -> None:
        """Reset rate limiter state for a specific key or all keys."""
        ...

    async def prune_expired(self, window: float | None = None) -> int:
        """Prune expired tokens and evict empty keys from the store."""
        return 0

    async def get_retry_after(self, key: str, window: float) -> float:
        """The earliest a permit for `key` can free, in seconds: a lower bound on when a refused
        call can be admitted, exact while the key holds no more permits than its limit."""
        return 0.0

    async def get_retry_after_for(self, key: str, calls: int, window: float) -> float:
        """When a call refused under a limit of `calls` per `window` can be admitted, in seconds.

        This is what a refusal tells the caller. The shipped limiters answer exactly, also when
        the key holds more permits than `calls` (a limit was lowered, or replicas raced), and
        answer `math.inf` for `calls` of 0 or less, which never admits a call. A custom
        limiter that overrides only `get_retry_after` is asked that instead, a lower bound.
        """
        return await self.get_retry_after(key, window)

    def __deepcopy__(self, memo: Any) -> RateLimiter:
        """Copy as itself: a limiter passed to `ResilienceExtension(limiter=...)` is shared.

        Every service built from the declaration then counts against one limiter, which is the
        point of a distributed one, and no `SharedDependency` wrapper is needed. A subclass that
        must not be shared overrides this.
        """
        return self


class InMemoryRateLimiter(RateLimiter):
    """Local sliding window rate limiter using monotonic timestamp queues.

    It belongs to one process and one event loop. Each operation holds an `asyncio.Lock` and none
    of them awaits while it does, so on one loop two calls cannot interleave with or without the
    lock; it is not a thread lock, and it does not make the limiter safe to share across loops.

    One timestamp per permit. A redelivered message spends another, as the `RateLimiter`
    docstring says of every limiter.

    ``max_keys`` is the size at which expired keys are swept, not a hard cap: once the table
    holds more than ``max_keys`` keys, an ``acquire`` drops every key whose window has passed.
    Keys still inside their window are never evicted, because evicting one forgets its callers'
    spent budget and lets whoever varies the partition key flood past the limit; so the table
    can exceed ``max_keys`` while that many distinct keys are live at once.
    """

    def __init__(self, max_keys: int = 10000) -> None:
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self.max_keys = max_keys

    @property
    def tracked_keys(self) -> int:
        """How many partition keys the table holds now, expired ones not yet swept included."""
        return len(self._windows)

    def health_details(self) -> dict[str, Any]:
        """A local limiter: its counts are this process's alone."""
        return {"backend": "memory", "status": "local", "tracked_keys": self.tracked_keys}

    def _prune_expired_locked(self, cutoff: float) -> int:
        """Evict empty queues and timestamps older than cutoff under lock."""
        empty_keys: list[str] = []
        for k, q in self._windows.items():
            while q and q[0] <= cutoff:
                q.popleft()
            if not q:
                empty_keys.append(k)
        for k in empty_keys:
            self._windows.pop(k, None)
        return len(empty_keys)

    async def prune_expired(self, window: float | None = None) -> int:
        """Prune expired tokens and evict empty keys from memory."""
        now = time.monotonic()
        async with self._lock:
            cutoff = (now - window) if window is not None else now
            return self._prune_expired_locked(cutoff)

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        now = time.monotonic()
        cutoff = now - window
        async with self._lock:
            if len(self._windows) > self.max_keys:
                self._prune_expired_locked(cutoff)

            q = self._windows[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) < calls:
                q.append(now)
                return True
            if not q:
                self._windows.pop(key, None)
            return False

    async def reset(self, key: str | None = None) -> None:
        async with self._lock:
            if key is None:
                self._windows.clear()
            else:
                self._windows.pop(key, None)

    async def get_retry_after(self, key: str, window: float) -> float:
        now = time.monotonic()
        cutoff = now - window
        async with self._lock:
            if key not in self._windows:
                return 0.0
            q = self._windows[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if not q:
                self._windows.pop(key, None)
                return 0.0
            return max(0.0, q[0] + window - now)

    async def get_retry_after_for(self, key: str, calls: int, window: float) -> float:
        if calls <= 0:
            return math.inf  # a limit of no calls never admits one
        now = time.monotonic()
        cutoff = now - window
        async with self._lock:
            if key not in self._windows:
                return 0.0
            q = self._windows[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if not q:
                self._windows.pop(key, None)
                return 0.0
            if len(q) < calls:
                return 0.0
            # A call is admitted once fewer than `calls` permits are live: when the permit
            # `calls` from the newest leaves the window. The deque is in the order of one clock.
            return max(0.0, q[len(q) - calls] + window - now)


#: How long a limiter whose bucket could not be opened waits before it tries to open it again.
#: Every dispatch would otherwise spend its own JetStream round trips on an open that fails.
BUCKET_REOPEN_SECONDS = 5.0


def _connection_closed(js: Any) -> bool:
    """Whether the connection a JetStream context was made on has been closed.

    Read from the context's connection (`_nc`, which nats-py does not expose by another name);
    anything without one, such as a test's stand-in, counts as open.
    """
    connection = getattr(js, "_nc", None)
    return bool(getattr(connection, "is_closed", False))


class KvRateLimiter(RateLimiter):
    """Distributed sliding-window limiting backed by NATS JetStream KV.

    The distributed decision fails closed by default. ``in_memory_fallback`` is
    an explicit availability tradeoff: each process then owns an independent
    budget while the backend is degraded, and that state is exposed through
    :meth:`health_details`.

    Entries are one per partition key and are not removed when their window passes, so a
    bucket fed by a caller-controlled key grows with the number of distinct keys ever seen.
    Two ways to bound it, both explicit because the right bound depends on every window that
    shares the bucket: ``bucket_ttl`` (seconds) is set when this limiter CREATES the bucket and
    expires an entry that long after its last write, so it must be at least the longest window
    any service using the bucket declares, or a limit is silently weakened; an existing bucket
    keeps the configuration it was created with. :meth:`prune_expired` deletes the entries whose
    timestamps have all left a given window, for a caller that sweeps on a schedule.

    The bucket is shared by every service that names it, so :meth:`reset` without a key clears
    all of their counters, not only this service's.

    A limiter that was given a JetStream context and could not open the bucket tries again at
    most once every ``BUCKET_REOPEN_SECONDS``; the dispatches in between are decided without a
    round trip, by the in-memory fallback when it is on and by refusing with
    `RateLimiterUnavailableError` when it is not.

    One timestamp per permit in the key's entry. A redelivered message spends another, and a
    permit spent on a message a later extension refuses is not returned, as the `RateLimiter`
    docstring says of every limiter.

    Timestamps in the bucket are wall-clock times (`time.time`), since they are compared across
    processes; the in-memory limiter uses a monotonic clock. So the window is as accurate as the
    replicas' clocks agree: a replica whose clock is ahead expires the others' entries early,
    which widens the limit by the skew, and a clock stepped backwards keeps entries alive for the
    length of the step. When the limiter falls back to memory it counts afresh in a local
    monotonic window, and what the bucket had recorded is not carried over.

    **A refusal is remembered.** Saying no used to read the key's whole timestamp list, twice
    (once to decide, once for the retry hint): at ``calls=1000`` about 53 KB received for about
    200 bytes sent, per refused request. The list only gains timestamps from other replicas and
    loses one only when it leaves the window, so once a key has been refused this limiter knows
    the moment a slot can next open (when enough counted timestamps have left the window) and
    answers ``False``, and the retry hint, from memory until then; the semantics are the exact
    sliding window as before. The price is a :meth:`reset` made on ANOTHER replica: it is seen
    when that moment passes, not before. This limiter's own :meth:`reset` clears what it
    remembers. ``deny_cache_size`` bounds how many keys are remembered (the oldest are
    dropped); ``0`` turns it off.
    """

    def __init__(
        self,
        kv: Any = None,
        js: Any = None,
        bucket_name: str = "rate_limits",
        in_memory_fallback: bool = False,
        max_retries: int = 5,
        retry_base_delay: float = 0.005,
        bucket_ttl: float | None = None,
        deny_cache_size: int = 10_000,
    ) -> None:
        self._kv = kv
        self._js = js
        self.bucket_name = bucket_name
        self._subject_prefix: str | None = None
        self._prefix_applied = False
        self.bucket_ttl = bucket_ttl
        self.deny_cache_size = deny_cache_size
        #: (hashed key, calls, window) -> the time before which the key stays refused.
        self._denied_until: dict[tuple[str, int, float], float] = {}
        #: (hashed key, window) -> the time the oldest counted timestamp leaves the window,
        #: which is what the retry hint reports.
        self._retry_at: dict[tuple[str, float], float] = {}
        #: Refusals answered from memory, with no read of the bucket.
        self.denied_locally = 0
        self.in_memory_fallback = in_memory_fallback
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._fallback = InMemoryRateLimiter()
        self._backend_status = "distributed" if kv is not None else "uninitialized"
        self._fallback_total = 0
        self._last_error_type: str | None = None
        #: The JetStream context the held bucket was opened from, or None for a bucket handed in.
        self._opened_with: Any = None
        #: Every JetStream context this limiter was given, newest last, held weakly: a limiter is
        #: shared by the services declared with it, and when the one whose connection its bucket
        #: belongs to stops, another one's is what it moves to.
        self._known_js: list[Any] = []
        #: `time.monotonic()` before which `acquire` does not try to open the bucket again.
        self._reopen_at = 0.0
        #: Why the last open failed, reported for the dispatches that wait out the interval.
        self._open_failure: BaseException | None = None

    def use_subject_prefix(self, prefix: str | None) -> None:
        """Name the bucket on the broker the way a `KvExtension` bucket is named.

        The service's subject prefix goes in front of the declared name (`px_rate_limits`), so two
        environments sharing a broker, or the isolated prefix a test session takes, do not share
        counters. Called by the extension that opens the limiter it was given, before `init_kv`; a
        limiter its owner opens, or hands a bucket, keeps the name it was given.

        A limiter names ONE bucket. Limiter instances are shared, not copied, by every service
        that is declared with one (a `SharedDependency` or a class attribute), so a second service
        with a DIFFERENT prefix would silently count in the first one's bucket, or move the first
        into its own. That is refused here, at setup; the same prefix again is fine.
        """
        prefix = prefix or None
        if self._prefix_applied and prefix != self._subject_prefix:
            raise ConfigurationError(
                f"this KvRateLimiter already names its bucket under subject prefix "
                f"{self._subject_prefix!r}, and a service with prefix {prefix!r} cannot share it: "
                f"the two would count in one bucket. Give each service its own KvRateLimiter."
            )
        self._subject_prefix = prefix
        self._prefix_applied = True

    @property
    def bucket_wire_name(self) -> str:
        """The name the bucket has on the broker: the declared name, behind the prefix if any."""
        prefix = self._subject_prefix
        return f"{prefix}_{self.bucket_name}" if prefix else self.bucket_name

    def health_details(self) -> dict[str, Any]:
        """Describe whether the configured global bound is currently authoritative."""
        return {
            "backend": "nats-kv",
            "status": self._backend_status,
            "fallback_enabled": self.in_memory_fallback,
            "fallback_total": self._fallback_total,
            "last_error_type": self._last_error_type,
        }

    def _mark_healthy(self) -> None:
        if self._backend_status in {"degraded", "unavailable"}:
            logger.info("KvRateLimiter distributed backend recovered")
        self._backend_status = "distributed"
        self._last_error_type = None
        self._open_failure = None

    def _mark_failure(self, exc: BaseException) -> None:
        status = "degraded" if self.in_memory_fallback else "unavailable"
        if self._backend_status != status:
            logger.warning(
                "KvRateLimiter distributed backend {} ({})",
                status,
                type(exc).__name__,
            )
        self._backend_status = status
        self._last_error_type = type(exc).__name__

    async def _fallback_or_raise(
        self, exc: BaseException, key: str, calls: int, window: float
    ) -> bool:
        self._mark_failure(exc)
        if not self.in_memory_fallback:
            raise RateLimiterUnavailableError(
                "distributed rate limiter could not make an authoritative decision"
            ) from exc
        self._fallback_total += 1
        return await self._fallback.acquire(key, calls, window)

    async def init_kv(self, js: Any = None, kv: Any = None) -> None:
        """Open or create the limiter bucket and record backend availability."""
        if kv is not None:
            self._kv = kv
            self._mark_healthy()
            return
        if js is not None:
            self._js = js
            self._remember(js)
            if self._opened_with is not None and _connection_closed(self._opened_with):
                # The bucket belongs to a connection that has closed (the service it served
                # stopped or restarted). A new service built from the same declaration is handed
                # this limiter, so it opens the bucket again on its own connection.
                if not _connection_closed(js):
                    self._kv = None
                    self._opened_with = None
        if self._kv is not None:
            self._mark_healthy()
            return
        if self._js is None:
            return
        try:
            try:
                self._kv = await self._js.key_value(self.bucket_wire_name)
            except nats.js.errors.BucketNotFoundError:
                try:
                    params: dict[str, Any] = {"bucket": self.bucket_wire_name}
                    if self.bucket_ttl is not None:
                        params["ttl"] = self.bucket_ttl
                    self._kv = await self._js.create_key_value(**params)
                except nats.js.errors.KeyWrongLastSequenceError:
                    self._kv = await self._js.key_value(self.bucket_wire_name)
        except Exception as exc:
            self._reopen_at = time.monotonic() + BUCKET_REOPEN_SECONDS
            self._open_failure = exc
            self._mark_failure(exc)
            if not self.in_memory_fallback:
                raise RateLimiterUnavailableError(
                    "distributed rate limiter bucket is unavailable"
                ) from exc
            return
        self._opened_with = self._js
        self._mark_healthy()

    def _remember(self, js: Any) -> None:
        """Note a JetStream context this limiter was given, without keeping its connection alive."""
        alive = [ref for ref in self._known_js if ref() is not None and ref() is not js]
        try:
            alive.append(weakref.ref(js))
        except TypeError:  # a stand-in that cannot be weakly referenced is held as it is
            alive.append(lambda js=js: js)
        self._known_js = alive

    def _live_js(self) -> Any:
        """The newest JetStream context this limiter was given whose connection is still open."""
        for ref in reversed(self._known_js):
            candidate = ref()
            if candidate is not None and not _connection_closed(candidate):
                return candidate
        return None

    def _safe_key(self, key: str) -> str:
        """Return an opaque, fixed-size KV key without retaining credential text."""
        return f"sha256_{_key_digest(key)}"

    @staticmethod
    def _timestamps(entry: Any) -> list[float]:
        try:
            decoded = json.loads(entry.value.decode("utf-8"))
        except Exception as exc:
            raise _RateLimitStateError("rate limit state is not valid JSON") from exc
        if not isinstance(decoded, list):
            raise _RateLimitStateError("rate limit state is not a timestamp list")
        timestamps: list[float] = []
        for value in decoded:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise _RateLimitStateError("rate limit state contains a non-numeric timestamp")
            timestamp = float(value)
            if not math.isfinite(timestamp):
                raise _RateLimitStateError("rate limit state contains a non-finite timestamp")
            timestamps.append(timestamp)
        return timestamps

    def _remember_refusal(
        self, safe_key: str, calls: int, window: float, valid_timestamps: list[float]
    ) -> None:
        """Note when a refused key can next be allowed, from the timestamps just read."""
        if self.deny_cache_size <= 0 or not valid_timestamps or calls <= 0:
            # A limit of no calls is refused on every call, and has no moment to remember.
            return
        ordered = sorted(valid_timestamps)
        # `len(ordered) - calls + 1` stamps must leave before the count is below `calls`; the
        # last of those is at index `len(ordered) - calls`.
        deny_until = ordered[len(ordered) - calls] + window
        denied_key, retry_key = (safe_key, calls, window), (safe_key, window)
        self._denied_until.pop(denied_key, None)
        self._retry_at.pop(retry_key, None)
        self._denied_until[denied_key] = deny_until
        self._retry_at[retry_key] = ordered[0] + window
        self._bound_denials()

    def _bound_denials(self) -> None:
        """Keep the remembered refusals within `deny_cache_size`: expired first, then oldest."""
        self._trim(self._denied_until)
        self._trim(self._retry_at)

    def _trim(self, table: dict[Any, float]) -> None:
        if len(table) <= self.deny_cache_size:
            return
        now = time.time()
        for stale in [k for k, until in table.items() if until <= now]:
            del table[stale]
        while len(table) > self.deny_cache_size:
            del table[next(iter(table))]

    def _forget_refusals(self, safe_key: str | None = None) -> None:
        if safe_key is None:
            self._denied_until.clear()
            self._retry_at.clear()
            return
        for denied in [k for k in self._denied_until if k[0] == safe_key]:
            del self._denied_until[denied]
        for retry in [k for k in self._retry_at if k[0] == safe_key]:
            del self._retry_at[retry]

    async def _retry_cas(self, attempt: int) -> None:
        ceiling = min(0.1, self.retry_base_delay * (2**attempt))
        await asyncio.sleep(ceiling * random.uniform(0.5, 1.5))

    async def prune_expired(self, window: float | None = None) -> int:
        """Delete the KV entries whose timestamps have all left ``window``, and prune the fallback.

        Returns how many entries were deleted from the bucket and the fallback together. With no
        ``window`` the bucket is left alone: an entry's window is the caller's, so nothing can be
        called expired without one. An entry that changes while it is being examined is kept.
        """
        removed = await self._fallback.prune_expired(window)
        if window is None or self._kv is None:
            return removed
        cutoff = time.time() - window
        try:
            for safe_key in await self._kv_keys():
                try:
                    entry = await self._kv.get(safe_key)
                    timestamps = self._timestamps(entry)
                except (nats.js.errors.KeyNotFoundError, nats.js.errors.KeyDeletedError):
                    continue
                except _RateLimitStateError:
                    continue  # not ours to judge: acquire reports it where it matters
                if any(timestamp > cutoff for timestamp in timestamps):
                    continue
                try:
                    await self._kv.delete(safe_key, last=entry.revision)
                except nats.js.errors.KeyWrongLastSequenceError:
                    continue
                removed += 1
        except Exception as exc:
            self._mark_failure(exc)
            if not self.in_memory_fallback:
                raise RateLimiterUnavailableError(
                    "distributed rate limiter could not prune its bucket"
                ) from exc
        return removed

    async def _kv_keys(self) -> list[str]:
        """Every key in the bucket; an empty bucket is not an error."""
        try:
            return list(await self._kv.keys())
        except nats.js.errors.NoKeysError:
            return []

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        if self._opened_with is not None and _connection_closed(self._opened_with):
            # The service whose connection the bucket was opened on has stopped; another service
            # sharing this limiter is still running on its own connection.
            live = self._live_js()
            if live is not None:
                try:
                    await self.init_kv(js=live)
                except RateLimiterUnavailableError as exc:
                    return await self._fallback_or_raise(exc, key, calls, window)
        if self._kv is None and self._js is not None and time.monotonic() >= self._reopen_at:
            try:
                await self.init_kv()
            except RateLimiterUnavailableError as exc:
                return await self._fallback_or_raise(exc, key, calls, window)

        if self._kv is None:
            if self.in_memory_fallback and self._backend_status == "degraded":
                self._fallback_total += 1
                return await self._fallback.acquire(key, calls, window)
            # A limiter waiting out a failed open reports that failure, not a missing bucket:
            # the bucket is configured, and the health detail keeps the error that was seen.
            return await self._fallback_or_raise(
                self._open_failure or RuntimeError("NATS KV bucket is not configured"),
                key,
                calls,
                window,
            )

        safe_key = self._safe_key(key)

        denied_key = (safe_key, calls, window)
        remembered = self._denied_until.get(denied_key)
        if remembered is not None:
            if time.time() < remembered:
                self.denied_locally += 1
                return False
            del self._denied_until[denied_key]

        try:
            for attempt in range(self.max_retries):
                try:
                    entry = await self._kv.get(safe_key)
                except (nats.js.errors.KeyNotFoundError, nats.js.errors.KeyDeletedError):
                    entry = None

                now = time.time()
                cutoff = now - window
                if entry is None:
                    if calls <= 0:
                        self._mark_healthy()
                        return False
                    try:
                        await self._kv.create(safe_key, json.dumps([now]).encode("utf-8"))
                    except nats.js.errors.KeyWrongLastSequenceError:
                        await self._retry_cas(attempt)
                        continue
                    self._mark_healthy()
                    return True

                timestamps = self._timestamps(entry)
                valid_timestamps = [timestamp for timestamp in timestamps if timestamp > cutoff]
                if len(valid_timestamps) >= calls:
                    if len(valid_timestamps) < len(timestamps):
                        try:
                            await self._kv.update(
                                safe_key,
                                json.dumps(valid_timestamps).encode("utf-8"),
                                last=entry.revision,
                            )
                        except nats.js.errors.KeyWrongLastSequenceError:
                            pass
                    self._remember_refusal(safe_key, calls, window, valid_timestamps)
                    self._mark_healthy()
                    return False

                valid_timestamps.append(now)
                try:
                    await self._kv.update(
                        safe_key,
                        json.dumps(valid_timestamps).encode("utf-8"),
                        last=entry.revision,
                    )
                except nats.js.errors.KeyWrongLastSequenceError:
                    await self._retry_cas(attempt)
                    continue
                self._mark_healthy()
                return True
            raise _CasExhaustedError(f"CAS did not converge after {self.max_retries} attempts")
        except Exception as exc:
            return await self._fallback_or_raise(exc, key, calls, window)

    async def get_retry_after(self, key: str, window: float) -> float:
        """The earliest a stamp of `key` leaves the window: a lower bound on when a refused call
        can be admitted, exact while the bucket holds no more stamps than the limit."""
        return await self._retry_hint(key, window, None)

    async def get_retry_after_for(self, key: str, calls: int, window: float) -> float:
        """When a call refused under `calls` per `window` can be admitted: once fewer than
        `calls` stamps are live, which is when the stamp `calls` from the newest leaves."""
        return await self._retry_hint(key, window, calls)

    async def _retry_hint(self, key: str, window: float, calls: int | None) -> float:
        """`get_retry_after` (`calls` None) and `get_retry_after_for`, without exposing the key."""

        if calls is not None and calls <= 0:
            return math.inf  # a limit of no calls never admits one

        async def fallback() -> float:
            if calls is None:
                return await self._fallback.get_retry_after(key, window)
            return await self._fallback.get_retry_after_for(key, calls, window)

        if self._backend_status == "degraded" or self._kv is None:
            if self.in_memory_fallback:
                return await fallback()
            raise RateLimiterUnavailableError("distributed rate limiter is unavailable")

        safe_key = self._safe_key(key)
        # A refusal remembers both: when a stamp frees, and when the count drops below the limit.
        table: dict[Any, float] = self._retry_at if calls is None else self._denied_until
        remembered_key: Any = (safe_key, window) if calls is None else (safe_key, calls, window)
        retry_at = table.get(remembered_key)
        if retry_at is not None:
            remaining = retry_at - time.time()
            if remaining > 0:
                return remaining
            del table[remembered_key]
        try:
            entry = await self._kv.get(safe_key)
        except (nats.js.errors.KeyNotFoundError, nats.js.errors.KeyDeletedError):
            self._mark_healthy()
            return 0.0
        except Exception as exc:
            self._mark_failure(exc)
            if self.in_memory_fallback:
                return await fallback()
            raise RateLimiterUnavailableError("distributed rate limiter is unavailable") from exc

        try:
            timestamps = self._timestamps(entry)
        except Exception as exc:
            self._mark_failure(exc)
            if self.in_memory_fallback:
                return await fallback()
            raise RateLimiterUnavailableError("distributed rate limiter state is invalid") from exc
        cutoff = time.time() - window
        # Replicas append in the order they write, not the order of their clocks.
        valid = sorted(timestamp for timestamp in timestamps if timestamp > cutoff)
        self._mark_healthy()
        if not valid or (calls is not None and len(valid) < calls):
            return 0.0
        frees = valid[0] if calls is None else valid[len(valid) - calls]
        return max(0.0, frees + window - time.time())

    async def reset(self, key: str | None = None) -> None:
        """Clear one key's counter, or every counter in the bucket when ``key`` is None.

        Also forgets this limiter's remembered refusals. Another replica's limiter forgets
        them only when each one's moment passes.
        """
        self._forget_refusals(self._safe_key(key) if key is not None else None)
        if self._kv is not None:
            try:
                targets = [self._safe_key(key)] if key is not None else await self._kv_keys()
                for safe_key in targets:
                    try:
                        await self._kv.delete(safe_key)
                    except (nats.js.errors.KeyNotFoundError, nats.js.errors.KeyDeletedError):
                        pass
            except Exception as exc:
                self._mark_failure(exc)
                if not self.in_memory_fallback:
                    raise RateLimiterUnavailableError(
                        "distributed rate limiter could not reset state"
                    ) from exc
        await self._fallback.reset(key)


def check_a_limit(calls: Any, window: Any, *, where: str) -> None:
    """Refuse a limit that cannot be what its author meant, where it is declared.

    `calls` is a whole number of at least one: zero or less refuses every call, and a bool or a
    fraction is not a count. `window` is a finite number of seconds above zero: a window of zero or
    less lets every call through, so the limit never limits, and a window that is not finite is
    never over. None of these raises when the limiter runs, so each is a limit that looks declared
    and does something else.
    """
    if isinstance(calls, bool) or not isinstance(calls, int) or calls < 1:
        raise ConfigurationError(
            f"{where}: calls must be a whole number of at least 1, got {calls!r} "
            f"({type(calls).__name__})"
        )
    if (
        isinstance(window, bool)
        or not isinstance(window, int | float)
        or not math.isfinite(window)
        or window <= 0
    ):
        raise ConfigurationError(
            f"{where}: window must be a finite number of seconds greater than 0, got {window!r} "
            f"({type(window).__name__})"
        )


@dataclass
class RateLimitConfig:
    """Configuration associated with a @rate_limit decorated handler.

    `calls` and `window` are checked when it is built (see `check_a_limit`); a limit that cannot
    work raises `ConfigurationError`.
    """

    calls: int
    window: float
    key: Callable[..., str] | str | None = None
    key_source: Literal["header", "payload", "context"] | None = None
    limiter: RateLimiter | None = None

    def __post_init__(self) -> None:
        check_a_limit(self.calls, self.window, where="rate limit")
        if self.key_source not in {None, "header", "payload", "context"}:
            raise ValueError("key_source must be header, payload, or context")
        if self.key is None and self.key_source is not None:
            raise ValueError("key_source requires a rate-limit key")
        if isinstance(self.key, str) and self.key_source == "context":
            raise ValueError("string rate-limit keys use a header or payload source")
        if callable(self.key) and self.key_source == "header":
            raise ValueError("callable rate-limit keys receive the context or payload")

    @staticmethod
    def _handler_view(ctx: WorkerContext) -> WorkerContext:
        """`ctx` as the handler sees it: an event envelope's `payload` is its `data`.

        The event dispatcher marks an envelope in `ctx.data["envelope"]`, so
        this does not repeat its detection.
        """
        if "envelope" in ctx.data and isinstance(ctx.payload, dict) and "data" in ctx.payload:
            return replace(ctx, payload=ctx.payload["data"])
        return ctx

    def _key_from(self, value: Any) -> str:
        """A partition key function's answer as the key, or a refusal if it names no caller.

        `None` is what a function returns for a request that lacks the field it partitions by,
        and `str(None)` would put every such request in one bucket called "None": a global
        limit under a per-caller label.
        """
        if value is None:
            raise RateLimitKeyError(
                "the rate-limit key function returned None, which names no caller; "
                "return a string, or raise to refuse the request"
            )
        return str(value)

    def resolve_key(
        self,
        ctx: WorkerContext | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """Resolve the rate limit key from a WorkerContext or raw arguments.

        String keys use one declared authority. Headers are the secure default;
        caller-controlled payload partitioning requires ``key_source="payload"``.
        A missing value raises instead of silently creating a shared bucket.
        Callable keys receive the context by default or the payload when
        ``key_source="payload"``; exceptions from the callable reach the
        fail-closed extension boundary.
        """
        if ctx is not None:
            view = self._handler_view(ctx)
            if callable(self.key):
                argument = view.payload if self.key_source == "payload" else view
                return self._key_from(self.key(argument))
            if isinstance(self.key, str):
                source = self.key_source or "header"
                if source == "header":
                    expected = self.key.casefold()
                    for name, value in (ctx.headers or {}).items():
                        if name.casefold() == expected:
                            return str(value)
                elif source == "payload":
                    payload = view.payload
                    if isinstance(payload, dict) and self.key in payload:
                        return str(payload[self.key])
                raise RateLimitKeyError(
                    f"rate-limit key {self.key!r} is missing from the declared {source} source"
                )
            return ctx.data.get("handler_name") or ctx.subject or "global"

        if callable(self.key):
            return self._key_from(self.key(*args, **kwargs))
        if isinstance(self.key, str):
            source = self.key_source or "header"
            if source == "payload" and kwargs and self.key in kwargs:
                return str(kwargs[self.key])
            raise RateLimitKeyError(
                f"rate-limit key {self.key!r} is missing from the declared {source} source"
            )
        return "global"


def rate_limit(
    calls: int,
    window: float,
    key: Callable[..., str] | str | None = None,
    key_source: Literal["header", "payload", "context"] | None = None,
    limiter: RateLimiter | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to apply rate limiting to RPC or event handlers.

    Attaches RateLimitConfig metadata to the handler for ResilienceExtension,
    while also wrapping the function to support standalone direct calls.

    The wrapper is a coroutine function, because taking a permit is awaited: decorating a
    synchronous function turns it into one, and every caller must then `await` it.

    Args:
        calls: Max allowed calls within the time window, a whole number of at least 1.
        window: Window size in seconds, a finite number above 0. A `calls` or `window` that
            cannot work raises `ConfigurationError` where the decorator is applied.
        key: Optional callable or field name used to partition rate limiting.
        key_source: Authoritative input. String keys use ``header`` by default
            or ``payload`` explicitly. Callable keys receive ``context`` by
            default or ``payload`` explicitly.
        limiter: Optional custom RateLimiter instance.
    """

    check_a_limit(calls, window, where="@rate_limit")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        config = RateLimitConfig(
            calls=calls,
            window=window,
            key=key,
            key_source=key_source,
            limiter=limiter,
        )
        default_limiter = limiter or InMemoryRateLimiter()

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # If already evaluated by ResilienceExtension in worker_setup, pass through
            if id(config) in _rate_limit_checked.get():
                if inspect.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                return func(*args, **kwargs)

            # Standalone direct invocation: check rate limit
            resolved_key = config.resolve_key(None, *args, **kwargs)
            active_limiter = config.limiter or default_limiter
            allowed = await active_limiter.acquire(resolved_key, config.calls, config.window)
            if not allowed:
                retry_after = await active_limiter.get_retry_after_for(
                    resolved_key, config.calls, config.window
                )
                raise RateLimitExceeded(
                    "rate limit exceeded",
                    details={
                        "key": key_fingerprint(resolved_key),
                        "calls": config.calls,
                        "window": config.window,
                    },
                    retry_after=retry_after,
                )

            if inspect.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            return func(*args, **kwargs)

        # `functools.wraps` copies the handler's markers (`_cliffracer_rpc`, the event, durable and
        # fanout ones) onto the wrapper; this one is the limiter's own.
        setattr(wrapper, "_cliffracer_rate_limit", config)  # noqa: B010
        return wrapper

    return decorator

"""Sliding window rate limiting and call rejection.

Provides sliding window rate limiting backends (in-memory and NATS KV distributed),
the @rate_limit decorator, and the RateLimitExceeded rejection exception.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import re
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from cliffracer.core.extension import RejectMessage

if TYPE_CHECKING:
    from cliffracer.core.extension import WorkerContext

_rate_limit_checked: ContextVar[bool] = ContextVar("_rate_limit_checked", default=False)


class RateLimitExceeded(RejectMessage):
    """Raised when a request exceeds the configured rate limit.

    Inherits from RejectMessage so core container's worker loop catches it
    in worker_setup, skips the handler, and translates it to wire error:
        {"error": "refused: rate limit exceeded", ...}
    surfacing on the caller side as an RpcRefused exception.
    """

    def __init__(
        self,
        reason: str = "rate limit exceeded",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.details = details or {}


class RateLimiter(ABC):
    """Abstract base class for sliding window rate limiters."""

    @abstractmethod
    async def acquire(self, key: str, calls: int, window: float) -> bool:
        """Attempt to acquire a call permit under the sliding window.

        Args:
            key: Rate limit partition key
            calls: Maximum allowed calls within the window
            window: Time window in seconds

        Returns:
            True if permitted, False if limit exceeded.
        """
        ...

    @abstractmethod
    async def reset(self, key: str | None = None) -> None:
        """Reset rate limiter state for a specific key or all keys."""
        ...

    def __deepcopy__(self, memo: Any) -> RateLimiter:
        """Preserve explicitly shared RateLimiter instances across service extensions."""
        return self


class InMemoryRateLimiter(RateLimiter):
    """Local sliding window rate limiter using monotonic timestamp queues with locks."""

    def __init__(self) -> None:
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        now = time.monotonic()
        cutoff = now - window
        async with self._lock:
            q = self._windows[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) < calls:
                q.append(now)
                return True
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
            q = self._windows[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if q:
                return max(0.0, q[0] + window - now)
            return 0.0


class KvRateLimiter(RateLimiter):
    """Distributed sliding window rate limiter using NATS JetStream KV store.

    Uses optimistic concurrency control (CAS) via revision checks to maintain
    distributed timestamp lists. Falls back gracefully to local in-memory
    limiting if the KV store is unreachable.
    """

    def __init__(
        self,
        kv: Any = None,
        js: Any = None,
        bucket_name: str = "rate_limits",
        in_memory_fallback: bool = True,
        max_retries: int = 5,
    ) -> None:
        self._kv = kv
        self._js = js
        self.bucket_name = bucket_name
        self.in_memory_fallback = in_memory_fallback
        self.max_retries = max_retries
        self._fallback = InMemoryRateLimiter()

    async def init_kv(self, js: Any = None, kv: Any = None) -> None:
        """Initialize or connect to the NATS KV bucket."""
        if kv is not None:
            self._kv = kv
            return
        if js is not None:
            self._js = js
        if self._kv is None and self._js is not None:
            try:
                self._kv = await self._js.key_value(self.bucket_name)
            except Exception:
                try:
                    self._kv = await self._js.create_key_value(bucket=self.bucket_name)
                except Exception as exc:
                    logger.warning(
                        f"KvRateLimiter could not open/create bucket '{self.bucket_name}': {exc}"
                    )

    def _safe_key(self, key: str) -> str:
        # NATS KV keys only allow alphanumeric, ., _, -, /, =
        safe = re.sub(r"[^a-zA-Z0-9._\-/=]", "_", key)
        return safe or "default"

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        if self._kv is None and self._js is not None:
            await self.init_kv()

        if self._kv is None:
            if self.in_memory_fallback:
                return await self._fallback.acquire(key, calls, window)
            raise RuntimeError("KvRateLimiter: No NATS KV bucket configured")

        safe_key = self._safe_key(key)
        now = time.time()
        cutoff = now - window

        for _ in range(self.max_retries):
            try:
                try:
                    entry = await self._kv.get(safe_key)
                except Exception:
                    entry = None

                if entry is not None and entry.value:
                    try:
                        timestamps = json.loads(entry.value.decode("utf-8"))
                        if not isinstance(timestamps, list):
                            timestamps = []
                    except Exception:
                        timestamps = []

                    valid_timestamps = [t for t in timestamps if t > cutoff]
                    if len(valid_timestamps) >= calls:
                        return False

                    valid_timestamps.append(now)
                    payload = json.dumps(valid_timestamps).encode("utf-8")
                    try:
                        await self._kv.update(safe_key, payload, last=entry.revision)
                        return True
                    except Exception:
                        # CAS revision mismatch, retry with fresh entry
                        continue
                else:
                    payload = json.dumps([now]).encode("utf-8")
                    try:
                        await self._kv.create(safe_key, payload)
                        return True
                    except Exception:
                        # Concurrent creation race, retry
                        continue

            except Exception as exc:
                if self.in_memory_fallback:
                    logger.warning(
                        f"KvRateLimiter error on key '{key}': {exc}. Falling back to in-memory."
                    )
                    return await self._fallback.acquire(key, calls, window)
                raise

        if self.in_memory_fallback:
            return await self._fallback.acquire(key, calls, window)
        return False

    async def reset(self, key: str | None = None) -> None:
        if self._kv is not None and key is not None:
            safe_key = self._safe_key(key)
            try:
                await self._kv.delete(safe_key)
            except Exception:
                pass
        await self._fallback.reset(key)


@dataclass
class RateLimitConfig:
    """Configuration associated with a @rate_limit decorated handler."""

    calls: int
    window: float
    key: Callable[..., str] | str | None = None
    limiter: RateLimiter | None = None

    def resolve_key(
        self,
        ctx: WorkerContext | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """Resolve the rate limit key from a WorkerContext or raw arguments."""
        if ctx is not None:
            if callable(self.key):
                try:
                    return str(self.key(ctx))
                except TypeError:
                    return str(self.key(ctx.payload))
            if isinstance(self.key, str):
                if ctx.payload and self.key in ctx.payload:
                    return str(ctx.payload[self.key])
                if ctx.headers and self.key in ctx.headers:
                    return str(ctx.headers[self.key])
                return self.key
            return ctx.data.get("handler_name") or ctx.subject or "global"

        if callable(self.key):
            try:
                return str(self.key(*args, **kwargs))
            except Exception:
                pass
        if isinstance(self.key, str) and kwargs and self.key in kwargs:
            return str(kwargs[self.key])
        return "global"


def rate_limit(
    calls: int,
    window: float,
    key: Callable[..., str] | str | None = None,
    limiter: RateLimiter | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to apply rate limiting to RPC or event handlers.

    Attaches RateLimitConfig metadata to the handler for ResilienceExtension,
    while also wrapping the function to support standalone direct calls.

    Args:
        calls: Max allowed calls within the time window.
        window: Window size in seconds.
        key: Optional callable or key attribute name to partition rate limiting.
        limiter: Optional custom RateLimiter instance.
    """
    config = RateLimitConfig(calls=calls, window=window, key=key, limiter=limiter)
    default_limiter = limiter or InMemoryRateLimiter()

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # If already evaluated by ResilienceExtension in worker_setup, pass through
            if _rate_limit_checked.get():
                if inspect.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                return func(*args, **kwargs)

            # Standalone direct invocation: check rate limit
            resolved_key = config.resolve_key(None, *args, **kwargs)
            active_limiter = config.limiter or default_limiter
            allowed = await active_limiter.acquire(resolved_key, config.calls, config.window)
            if not allowed:
                raise RateLimitExceeded(
                    "rate limit exceeded",
                    details={
                        "key": resolved_key,
                        "calls": config.calls,
                        "window": config.window,
                    },
                )

            if inspect.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            return func(*args, **kwargs)

        setattr(wrapper, "_cliffracer_rate_limit", config)  # noqa: B010
        setattr(wrapper, "_rate_limit", config)  # noqa: B010
        # Preserve cliffracer handler markers
        for attr in ("_cliffracer_rpc", "_cliffracer_async_rpc", "_cliffracer_events"):
            if hasattr(func, attr):
                setattr(wrapper, attr, getattr(func, attr))
        return wrapper

    return decorator

"""ResilienceExtension for Cliffracer.

Enforces rate limits in the worker_setup hook before any handler executes.
If a rate limit is exceeded, raises RateLimitExceeded(RejectMessage) which
causes the core container to skip the handler and reply with wire refusal:
    {"error": "refused: rate limit exceeded", ...}
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from cliffracer.core.extension import Extension, WorkerContext
from cliffracer_resilience.rate_limiter import (
    InMemoryRateLimiter,
    KvRateLimiter,
    RateLimitConfig,
    RateLimiter,
    RateLimitExceeded,
    _rate_limit_checked,
)

if TYPE_CHECKING:
    from cliffracer.core.extension import ExtensionSetupContext


class ResilienceExtension(Extension):
    """Extension that enforces rate limiting on incoming RPC and event dispatches.

    Declared on the service:
        class Orders(CliffracerService):
            resilience = ResilienceExtension()

            @rpc
            @rate_limit(calls=10, window=60.0)
            async def create_order(self, items: list) -> dict: ...

    When a handler's rate limit is exceeded, worker_setup raises RateLimitExceeded.
    Core container skips execution of the handler and immediately returns wire refusal:
        {"error": "refused: rate limit exceeded", ...}
    """

    fails_closed: bool = True

    def __init__(
        self,
        limiter: RateLimiter | None = None,
        default_calls: int | None = None,
        default_window: float | None = None,
    ) -> None:
        self._custom_limiter = limiter
        self.limiter = limiter if limiter is not None else InMemoryRateLimiter()
        self.default_calls = default_calls
        self.default_window = default_window
        self._rate_limits: dict[str, RateLimitConfig] = {}
        self._event_rate_limits: dict[str, RateLimitConfig] = {}

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        """Scan service methods for @rate_limit decorators."""
        self._rate_limits = {}
        self._event_rate_limits = {}
        if self._custom_limiter is None:
            self.limiter = InMemoryRateLimiter()
        else:
            self.limiter = self._custom_limiter

        self._discover_rate_limits(ctx.service)
        if isinstance(self.limiter, KvRateLimiter) and hasattr(ctx.service, "js"):
            await self.limiter.init_kv(js=getattr(ctx.service, "js", None))

    async def start(self) -> None:
        """Connect to distributed KV bucket if using KvRateLimiter."""
        if isinstance(self.limiter, KvRateLimiter):
            js = getattr(self.service, "js", None)
            if js is not None:
                await self.limiter.init_kv(js=js)

    def _discover_rate_limits(self, service: Any) -> None:
        """Find all @rate_limit decorated methods on the service instance and class."""
        service_cls = type(service)
        for name in dir(service_cls):
            if name.startswith("__"):
                continue

            attr = getattr(service, name, None)
            if attr is None:
                continue

            config = getattr(attr, "_cliffracer_rate_limit", None)
            if config is not None and isinstance(config, RateLimitConfig):
                self._rate_limits[name] = config

                # Also map event subjects if this is an event listener
                if hasattr(attr, "_cliffracer_events"):
                    for pattern in getattr(attr, "_cliffracer_events", ()):
                        self._event_rate_limits[pattern] = config

    def _get_config(self, ctx: WorkerContext) -> RateLimitConfig | None:
        """Retrieve the RateLimitConfig applicable to the incoming WorkerContext."""
        handler_name = ctx.data.get("handler_name")
        if handler_name and handler_name in self._rate_limits:
            return self._rate_limits[handler_name]

        if ctx.subject and ctx.subject in self._event_rate_limits:
            return self._event_rate_limits[ctx.subject]

        # Check by subject matching
        if ctx.subject:
            # Strip service prefix if RPC subject, e.g. "orders.rpc.create_order"
            parts = ctx.subject.split(".")
            if len(parts) >= 3 and parts[1] == "rpc":
                rpc_name = parts[2]
                if rpc_name in self._rate_limits:
                    return self._rate_limits[rpc_name]

        # Check default limit if configured
        if self.default_calls is not None and self.default_window is not None:
            return RateLimitConfig(
                calls=self.default_calls,
                window=self.default_window,
                limiter=self.limiter,
            )

        return None

    async def worker_setup(self, ctx: WorkerContext) -> None:
        """Evaluate rate limits before handler execution."""
        config = self._get_config(ctx)
        if config is None:
            return

        key = config.resolve_key(ctx)
        active_limiter = config.limiter or self.limiter

        allowed = await active_limiter.acquire(
            key=key,
            calls=config.calls,
            window=config.window,
        )

        if not allowed:
            logger.warning(
                f"{self.name}: rate limit exceeded for key '{key}' "
                f"({config.calls} calls / {config.window}s)"
            )
            raise RateLimitExceeded(
                "rate limit exceeded",
                details={
                    "key": key,
                    "calls": config.calls,
                    "window": config.window,
                },
            )

        # Mark rate limit as checked to prevent duplicate execution in handler wrapper
        token = _rate_limit_checked.set(True)
        ctx.data["_rate_limit_token"] = token

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        """Clean up per-request context tokens."""
        token = ctx.data.pop("_rate_limit_token", None)
        if token is not None:
            _rate_limit_checked.reset(token)

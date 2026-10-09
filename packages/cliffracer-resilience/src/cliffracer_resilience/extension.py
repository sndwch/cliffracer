"""ResilienceExtension for Cliffracer.

Enforces rate limits in the worker_setup hook before any handler executes.
If an RPC rate limit is exceeded, RateLimitExceeded causes the core container
to skip the handler and reply with wire refusal:
    {"error": "refused: rate limit exceeded", ...}
Durable events are NAKed with the limiter's retry delay.
"""

from __future__ import annotations

import inspect
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext
from cliffracer_resilience.circuit_breaker import ResilientRpcProxy
from cliffracer_resilience.rate_limiter import (
    InMemoryRateLimiter,
    KvRateLimiter,
    RateLimitConfig,
    RateLimiter,
    RateLimitExceeded,
    RateLimitKeyError,
    _rate_limit_checked,
    check_a_limit,
    key_fingerprint,
)

if TYPE_CHECKING:
    from cliffracer.core.extension import ExtensionSetupContext


#: The dispatches a caller sends: the ones a default limit applies to.
_CALLER_KINDS = frozenset({"rpc", "async_rpc", "event"})


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
        # Given together or not at all, and a limit that can work: a default of one without the other
        # applied nothing, and one that cannot work (a window of zero) limited nothing.
        if (default_calls is None) != (default_window is None):
            given = "default_calls" if default_calls is not None else "default_window"
            raise ConfigurationError(
                f"ResilienceExtension takes default_calls and default_window together, and was "
                f"given only {given}: a default limit needs both"
            )
        if default_calls is not None:
            check_a_limit(default_calls, default_window, where="ResilienceExtension default limit")
        self._custom_limiter = limiter
        self.limiter = limiter if limiter is not None else InMemoryRateLimiter()
        self.default_calls = default_calls
        self.default_window = default_window
        self._rate_limits: dict[str, RateLimitConfig] = {}
        # Per handler, so the figures grow with the handlers a service declares and never with
        # the partition keys callers send.
        self._permitted: defaultdict[str, int] = defaultdict(int)
        self._refused: defaultdict[str, int] = defaultdict(int)

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        """Scan service methods for @rate_limit decorators, and install the limiter.

        The limiter is the `limiter=` given to the constructor, else a new in-memory one, so a
        `svc.resilience.limiter = ...` assigned after the service was built is replaced here, and
        a stop and start begins an in-memory limiter's windows empty. Pass the limiter to
        `ResilienceExtension(limiter=...)` to have it kept.
        """
        self._rate_limits = {}
        if self._custom_limiter is None:
            self.limiter = InMemoryRateLimiter()
        else:
            self.limiter = self._custom_limiter

        self._discover_rate_limits(ctx.service)
        if isinstance(self.limiter, KvRateLimiter):
            # Through the context defensively: a caller's stand-in may carry no config, and then
            # there is no prefix to apply, which is also what an unprefixed environment gets.
            self.limiter.use_subject_prefix(
                getattr(getattr(ctx, "service_config", None), "subject_prefix", None)
            )
        if isinstance(self.limiter, KvRateLimiter) and hasattr(ctx.service, "js"):
            await self.limiter.init_kv(js=getattr(ctx.service, "js", None))

    async def start(self) -> None:
        """Connect to distributed KV bucket if using KvRateLimiter."""
        if isinstance(self.limiter, KvRateLimiter):
            js = getattr(self.service, "js", None)
            if js is not None:
                await self.limiter.init_kv(js=js)

    def health_details(self) -> dict[str, Any]:
        """What the limits have decided, which backend decides, and each outbound circuit.

        `rate_limiter` says whether a distributed limiter is authoritative or degraded.
        `rate_limits` counts, per handler, the dispatches the limit let through and the ones it
        refused since the service started; a payload that validation refuses counts in
        `permitted`, because the limit let it through first. `circuits` has one entry per `ResilientRpcProxy` the service
        declares.
        """

        def describe(limiter: RateLimiter) -> dict[str, Any]:
            return limiter.health_details() or {
                "backend": type(limiter).__name__,
                "status": "unreported",
            }

        details: dict[str, Any] = {"rate_limiter": describe(self.limiter)}
        handler_limiters = {
            name: describe(config.limiter)
            for name, config in sorted(self._rate_limits.items())
            if config.limiter is not None and config.limiter is not self.limiter
        }
        if handler_limiters:
            details["handler_rate_limiters"] = handler_limiters
        handlers = sorted(set(self._permitted) | set(self._refused))
        details["rate_limits"] = {
            "permitted_total": sum(self._permitted.values()),
            "refused_total": sum(self._refused.values()),
            "by_handler": {
                name: {"permitted": self._permitted[name], "refused": self._refused[name]}
                for name in handlers
            },
        }
        circuits = self._circuits()
        if circuits:
            details["circuits"] = circuits
        return details

    def info_details(self) -> dict[str, Any]:
        """The limits this service enforces, by handler, and the limiter that counts them.

        A key is described by where it is read from, never by a value: a partition value can be
        a credential.
        """
        limits = {name: self._describe_limit(config) for name, config in self._rate_limits.items()}
        info: dict[str, Any] = {
            "rate_limiter": type(self.limiter).__name__,
            "limits": dict(sorted(limits.items())),
        }
        if self.default_calls is not None and self.default_window is not None:
            info["default_limit"] = {"calls": self.default_calls, "window": self.default_window}
        return info

    @staticmethod
    def _describe_limit(config: RateLimitConfig) -> dict[str, Any]:
        if callable(config.key):
            key = "function"
        elif isinstance(config.key, str):
            key = f"{config.key_source or 'header'}:{config.key}"
        else:
            key = "handler"
        limit: dict[str, Any] = {"calls": config.calls, "window": config.window, "key": key}
        if config.limiter is not None:
            limit["limiter"] = type(config.limiter).__name__
        return limit

    def _circuits(self) -> dict[str, dict[str, Any]]:
        """State of the breaker behind each `ResilientRpcProxy` declared on the service."""
        service = getattr(self, "service", None)
        if service is None:
            return {}
        declared = {
            attribute
            for klass in type(service).__mro__
            for attribute, value in vars(klass).items()
            if isinstance(value, ResilientRpcProxy)
        }
        circuits: dict[str, dict[str, Any]] = {}
        for attribute in sorted(declared):
            breaker = getattr(service, attribute).circuit_breaker
            circuits[attribute] = {
                "destination": breaker.name,
                "state": breaker.state.value,
                "failure_count": breaker.failure_count,
                "seconds_in_state": round(time.monotonic() - breaker.last_state_change, 3),
            }
        return circuits

    def _discover_rate_limits(self, service: Any) -> None:
        """Find the @rate_limit decorated methods defined on the service's class.

        Read from the class without evaluating anything: a property on the service is not run
        (one that opens a pool, or raises until `start()`, would otherwise fail startup from here),
        and a handler assigned on an instance is not found.
        """
        service_cls = type(service)
        for name in dir(service_cls):
            if name.startswith("__"):
                continue

            attr = inspect.getattr_static(service_cls, name, None)
            attr = getattr(attr, "__func__", attr)  # a staticmethod or classmethod wraps one
            if attr is None:
                continue

            config = getattr(attr, "_cliffracer_rate_limit", None)
            if config is not None and isinstance(config, RateLimitConfig):
                self._rate_limits[name] = config

    def _get_config(self, ctx: WorkerContext) -> RateLimitConfig | None:
        """Retrieve the RateLimitConfig applicable to the incoming WorkerContext."""
        handler_name = ctx.data.get("handler_name")
        if handler_name and handler_name in self._rate_limits:
            return self._rate_limits[handler_name]

        # The default is for what a caller sends. A `describe` request answers with no
        # authentication and is what a client's `verify=True` reads, and a timer or cron firing
        # has no caller to throttle; neither is a handler a limit was written for.
        if ctx.kind not in _CALLER_KINDS:
            return None

        # Check default limit if configured
        if self.default_calls is not None and self.default_window is not None:
            return RateLimitConfig(
                calls=self.default_calls,
                window=self.default_window,
                limiter=self.limiter,
            )

        return None

    def _counted_key(self, handler: str, config: RateLimitConfig, value: str) -> str:
        """The key a dispatch is counted under: this service, this handler, and the key's value.

        A limit belongs to the handler that declares it, so two handlers keyed on one header each
        count a caller on their own, and two services never count in one entry of a shared bucket
        however they name their handlers. The replicas of one service share a key, which is what a
        distributed limiter is for. The namespace is part of the service's identity on the broker.

        The key's value is left out when the handler declares no key: its name is the whole key.
        """
        service = getattr(getattr(self, "service", None), "config", None)
        scope = ".".join(
            part
            for part in (getattr(service, "namespace", None), getattr(service, "name", None))
            if part
        )
        counted = f"{scope}:{handler}" if scope else handler
        return counted if config.key is None else f"{counted}:{value}"

    async def worker_setup(self, ctx: WorkerContext) -> None:
        """Evaluate rate limits before handler execution."""
        config = self._get_config(ctx)
        if config is None:
            return

        label = ctx.data.get("handler_name") or "unknown"
        try:
            value = config.resolve_key(ctx)
        except RateLimitKeyError as missing:
            # A caller that left out the value its handler partitions by has sent an input the
            # limit cannot judge: that is a refusal, answered with what is missing, and a durable
            # event is acknowledged. Letting it escape made the extension's own check look broken,
            # an internal error for the caller and a message redelivered to the dead letters.
            self._refused[label] += 1
            self._service_log.warning(
                f"{self.name}: refused a message with no rate-limit key: {missing}"
            )
            raise RejectMessage(str(missing)) from missing
        key = self._counted_key(label, config, value)
        active_limiter = config.limiter or self.limiter

        allowed = await active_limiter.acquire(
            key=key,
            calls=config.calls,
            window=config.window,
        )

        if not allowed:
            self._refused[label] += 1
            retry_after = await active_limiter.get_retry_after_for(key, config.calls, config.window)
            fingerprint = key_fingerprint(value)
            self._service_log.warning(
                f"{self.name}: rate limit exceeded for key {fingerprint} "
                f"({config.calls} calls / {config.window}s)"
            )
            raise RateLimitExceeded(
                "rate limit exceeded",
                details={
                    "key": fingerprint,
                    "calls": config.calls,
                    "window": config.window,
                },
                retry_after=retry_after,
            )

        self._permitted[label] += 1
        # Mark rate limit as checked to prevent duplicate execution in handler wrapper
        token = _rate_limit_checked.set(_rate_limit_checked.get() | {id(config)})
        ctx.data["_rate_limit_token"] = token

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        """Clean up per-request context tokens."""
        token = ctx.data.pop("_rate_limit_token", None)
        if token is not None:
            _rate_limit_checked.reset(token)

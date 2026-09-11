"""Circuit breaker state machine for RPC proxies.

Transitions through CLOSED, OPEN, and HALF_OPEN states to fail fast locally
when downstream services exceed consecutive failure thresholds.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, TypeVar, cast, overload

from cliffracer.core.exceptions import (
    ConnectionError as CliffracerConnectionError,
)
from cliffracer.core.exceptions import (
    RpcError,
)
from cliffracer.rpc_proxy import MethodProxy, RpcProxy, ServiceProxy

T = TypeVar("T")

DEFAULT_MONITORED_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RpcError,
    CliffracerConnectionError,
)


class CircuitState(str, Enum):
    """Lifecycle state of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


CLOSED = CircuitState.CLOSED
OPEN = CircuitState.OPEN
HALF_OPEN = CircuitState.HALF_OPEN


class CircuitBreakerError(Exception):
    """Base exception for all circuit breaker errors."""


class RpcCircuitOpenError(CircuitBreakerError):
    """Raised when an RPC invocation is rejected because the circuit is OPEN."""

    def __init__(
        self,
        message: str = "Circuit breaker is open",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = details or {}


@dataclass
class CircuitBreakerConfig:
    """Configuration for a CircuitBreaker."""

    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 1
    monitored_exceptions: tuple[type[BaseException], ...] | list[type[BaseException]] = field(
        default_factory=lambda: DEFAULT_MONITORED_EXCEPTIONS
    )

    def __post_init__(self) -> None:
        if isinstance(self.monitored_exceptions, list):
            self.monitored_exceptions = tuple(self.monitored_exceptions)


class CircuitBreaker:
    """State machine maintaining resilience health for a remote service or endpoint.

    Requests execute normally in CLOSED state. Consecutive failures increment
    failure_count until reaching failure_threshold, tripping the circuit to OPEN.
    In OPEN state, calls reject locally with RpcCircuitOpenError without wire
    traffic. After recovery_timeout elapses, the circuit enters HALF_OPEN, allowing
    a single probe call whose outcome either resets to CLOSED or trips back to OPEN.
    """

    def __init__(
        self,
        name: str = "default",
        config: CircuitBreakerConfig | None = None,
    ) -> None:
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._opened_at = 0.0
        self._last_state_change = time.monotonic()
        self._last_failure: BaseException | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Current state of the circuit breaker, taking cooldown expiry into account."""
        if self._state == CircuitState.OPEN:
            now = time.monotonic()
            if now - self._opened_at >= self.config.recovery_timeout:
                return CircuitState.HALF_OPEN
            return CircuitState.OPEN
        return self._state

    @property
    def is_closed(self) -> bool:
        return self.state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    @property
    def is_half_open(self) -> bool:
        return self.state == CircuitState.HALF_OPEN

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def success_count(self) -> int:
        return self._success_count

    @property
    def last_failure(self) -> BaseException | None:
        return self._last_failure

    @property
    def last_state_change(self) -> float:
        return self._last_state_change

    def _is_monitored_exception(self, exc: BaseException) -> bool:
        """Check if an exception should contribute to circuit tripping."""
        if isinstance(exc, tuple(self.config.monitored_exceptions)):
            return True
        if isinstance(exc, RpcError):
            return True
        return False

    async def __aenter__(self) -> CircuitBreaker:
        """Check circuit state and permit call or fail fast with RpcCircuitOpenError."""
        async with self._lock:
            current = self.state
            if current == CircuitState.OPEN:
                raise RpcCircuitOpenError(
                    f"Circuit breaker '{self.name}' is OPEN",
                    details={
                        "name": self.name,
                        "state": "open",
                        "failure_count": self._failure_count,
                        "recovery_timeout": self.config.recovery_timeout,
                    },
                )
            if current == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.config.half_open_max_calls:
                    raise RpcCircuitOpenError(
                        f"Circuit breaker '{self.name}' is HALF-OPEN (probe in progress)",
                        details={
                            "name": self.name,
                            "state": "half_open",
                            "half_open_calls": self._half_open_calls,
                        },
                    )
                self._half_open_calls += 1
                self._state = CircuitState.HALF_OPEN
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> Literal[False]:
        if exc_val is None:
            await self.record_success()
            return False

        if self._is_monitored_exception(exc_val):
            await self.record_failure(exc_val)
        return False

    async def call(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Execute an async callable guarded by the circuit breaker."""
        async with self:
            return await func(*args, **kwargs)

    async def record_success(self) -> None:
        """Record a successful call and transition state if in HALF_OPEN."""
        async with self._lock:
            self._success_count += 1
            if self._state == CircuitState.HALF_OPEN or self.state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._half_open_calls = 0
                self._last_state_change = time.monotonic()
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    async def record_failure(self, exc: BaseException | None = None) -> None:
        """Record a failed call, incrementing failure count or tripping circuit."""
        async with self._lock:
            now = time.monotonic()
            self._last_failure = exc
            if self._state == CircuitState.HALF_OPEN or self.state == CircuitState.HALF_OPEN:
                # Probe failed, trip immediately back to OPEN
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._last_state_change = now
                self._half_open_calls = 0
            else:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    self._state = CircuitState.OPEN
                    self._opened_at = now
                    self._last_state_change = now
                    self._half_open_calls = 0

    def reset(self) -> None:
        """Reset the circuit breaker to CLOSED with 0 failures."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_calls = 0
        self._last_failure = None
        self._last_state_change = time.monotonic()

    def trip(self) -> None:
        """Forcefully trip the circuit breaker to OPEN."""
        now = time.monotonic()
        self._state = CircuitState.OPEN
        self._opened_at = now
        self._last_state_change = now
        self._half_open_calls = 0


class CircuitBreakerRegistry:
    """Registry managing circuit breakers by destination name."""

    def __init__(self, default_config: CircuitBreakerConfig | None = None) -> None:
        self.default_config = default_config or CircuitBreakerConfig()
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str, config: CircuitBreakerConfig | None = None) -> CircuitBreaker:
        """Get existing circuit breaker or create a new one."""
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name=name, config=config or self.default_config)
        return self._breakers[name]

    def reset_all(self) -> None:
        """Reset all registered circuit breakers."""
        for breaker in self._breakers.values():
            breaker.reset()


class ResilientMethodProxy(MethodProxy):
    """MethodProxy wrapped with circuit breaker protection."""

    def __init__(
        self,
        service_instance: Any,
        service_name: str,
        method_name: str,
        namespace: str | None = None,
        *,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        super().__init__(service_instance, service_name, method_name, namespace)
        self._circuit_breaker = circuit_breaker

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    async def __call__(self, **kwargs: Any) -> Any:
        async with self._circuit_breaker:
            return await super().__call__(**kwargs)

    def call_async(self, **kwargs: Any) -> Coroutine[Any, Any, Any]:
        """Fire-and-forget RPC dispatch guarded by circuit state.

        Evaluates circuit state before dispatch. When OPEN, raises
        RpcCircuitOpenError without network I/O. When CLOSED or HALF_OPEN,
        returns the unawaited coroutine from MethodProxy.call_async. The
        invocation does not record success or failure on the circuit breaker
        because fire-and-forget calls receive no reply from the remote service.
        """
        if self._circuit_breaker.state == CircuitState.OPEN:
            raise RpcCircuitOpenError(
                f"Circuit breaker '{self._circuit_breaker.name}' is OPEN",
                details={"service": self._service_name, "state": "open"},
            )
        return cast(Coroutine[Any, Any, Any], super().call_async(**kwargs))


class ResilientServiceProxy(ServiceProxy):
    """ServiceProxy returning ResilientMethodProxy instances."""

    def __init__(
        self,
        service_instance: Any,
        service_name: str,
        namespace: str | None = None,
        *,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        super().__init__(service_instance, service_name, namespace)
        self.circuit_breaker = circuit_breaker

    def __getattr__(self, method_name: str) -> ResilientMethodProxy:
        if method_name.startswith("_"):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{method_name}'")

        instance = self._service_instance()
        if instance is None:
            raise RuntimeError("Service instance was garbage collected")
        return ResilientMethodProxy(
            instance,
            self._service_name,
            method_name,
            self._namespace,
            circuit_breaker=self.circuit_breaker,
        )


class ResilientRpcProxy(RpcProxy):
    """Descriptor providing a resilient proxy to another service with integrated Circuit Breaker.

    Usage:
        class OrderService(CliffracerService):
            inventory = ResilientRpcProxy(
                "inventory_service",
                config=CircuitBreakerConfig(failure_threshold=3, recovery_timeout=10.0),
            )

            @rpc
            async def create_order(self, items: list) -> dict:
                # Fails fast with RpcCircuitOpenError when inventory circuit is OPEN
                avail = await self.inventory.check_stock(items=items)
                return {"status": "ok", "items": avail}
    """

    def __init__(
        self,
        service_name: str,
        namespace: str | None = None,
        *,
        circuit_breaker: CircuitBreaker | None = None,
        config: CircuitBreakerConfig | None = None,
    ) -> None:
        super().__init__(service_name, namespace=namespace)
        self.config = config or CircuitBreakerConfig()
        self._custom_circuit_breaker = circuit_breaker
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            name=service_name, config=self.config
        )

    @overload
    def __get__(self, instance: None, owner: type | None = ...) -> ResilientRpcProxy: ...

    @overload
    def __get__(self, instance: object, owner: type | None = ...) -> ResilientServiceProxy: ...

    def __get__(
        self, instance: Any, owner: type | None = None
    ) -> ResilientServiceProxy | ResilientRpcProxy:
        if instance is None:
            return self

        if instance not in self._proxies:
            cb = self._custom_circuit_breaker or CircuitBreaker(
                name=self.service_name, config=self.config
            )
            self._proxies[instance] = ResilientServiceProxy(
                instance,
                self.service_name,
                self._namespace,
                circuit_breaker=cb,
            )

        return self._proxies[instance]  # type: ignore[return-value]

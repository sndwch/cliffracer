"""Circuit breaker state machine for RPC proxies.

Transitions through CLOSED, OPEN, and HALF_OPEN states to fail fast locally
when downstream services exceed consecutive failure thresholds.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, TypeVar, overload

from cliffracer.core.exceptions import (
    RpcConnectionError,
    RpcNoRespondersError,
    RpcServerError,
    RpcTimeoutError,
)
from cliffracer.rpc_proxy import MethodProxy, RpcProxy, ServiceProxy

T = TypeVar("T")

# The errors that mean the dependency is failing or unreachable. The rest of the
# RPC hierarchy -- invalid arguments, an unknown method, a refusal by policy, an
# out-of-date client -- means the dependency answered and the caller is wrong,
# so none of it counts toward opening a circuit. A refusal in particular is how
# a rate-limited dependency sheds load, and that must not cut it off.
DEFAULT_MONITORED_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RpcTimeoutError,
    RpcNoRespondersError,
    RpcConnectionError,
    RpcServerError,
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
        if self.failure_threshold < 1:
            raise ValueError(
                f"failure_threshold must be at least 1, got {self.failure_threshold}: a smaller "
                f"one trips on the first failure, which is what 1 says"
            )
        if not self.recovery_timeout >= 0:
            raise ValueError(
                f"recovery_timeout must be 0 or more seconds, got {self.recovery_timeout}"
            )
        if self.half_open_max_calls < 1:
            raise ValueError(
                f"half_open_max_calls must be at least 1, got {self.half_open_max_calls}: with "
                f"none, every probe is refused and the circuit can never close"
            )
        monitored = self.monitored_exceptions
        if isinstance(monitored, type) or not all(
            isinstance(kind, type) and issubclass(kind, BaseException) for kind in monitored
        ):
            raise TypeError(
                f"monitored_exceptions must be a tuple or list of exception classes, got "
                f"{monitored!r}; a single class goes in a tuple: ({monitored!r},)"
            )


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
        self._half_open_generation = 0
        #: Bumped by `reset()`, `trip()` and a recovery (a probe closing the circuit). A call
        #: admitted while CLOSED carries the value it saw, and its result is not counted toward the
        #: failure run once the circuit has been moved or has recovered since, so evidence from
        #: before a recovery cannot decide the circuit that follows it. The circuit cannot be
        #: CLOSED again without one of the three, so a call from an earlier era is always behind.
        self._era = 0
        self._entry_probes: ContextVar[tuple[tuple[int | None, int], ...]] = ContextVar(
            f"circuit_breaker_{id(self)}_entry_probes", default=()
        )

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
        """When the circuit last changed state, on the `time.monotonic` clock.

        An OPEN circuit whose cooldown has passed is HALF_OPEN, and that change happened when the
        cooldown ended, so that is the time reported, whether or not a call has arrived to move
        the stored state.
        """
        if self._state == CircuitState.OPEN and self.state == CircuitState.HALF_OPEN:
            return self._opened_at + self.config.recovery_timeout
        return self._last_state_change

    def _is_monitored_exception(self, exc: BaseException) -> bool:
        """Check if an exception should contribute to circuit tripping."""
        return isinstance(exc, tuple(self.config.monitored_exceptions))

    def _open_error(self) -> RpcCircuitOpenError:
        """The refusal an OPEN circuit raises, built without touching the lock."""
        return RpcCircuitOpenError(
            f"Circuit breaker '{self.name}' is OPEN",
            details={
                "name": self.name,
                "state": "open",
                "failure_count": self._failure_count,
                "recovery_timeout": self.config.recovery_timeout,
            },
        )

    async def __aenter__(self) -> CircuitBreaker:
        """Check circuit state and permit call or fail fast with RpcCircuitOpenError.

        THE OPEN CHECK RUNS BEFORE THE LOCK, and that is the point of it. Failing
        fast, locally, without wire traffic is the whole promise of an open
        circuit; acquiring first made it the one path that waits for a lock it
        does not need. The branch mutates nothing -- `state` is a pure function
        of `_state`, `_opened_at` and `config.recovery_timeout` -- so there is
        nothing here for the lock to protect.

        The lock is kept for HALF_OPEN, which reads `_half_open_calls`, compares
        it and increments it. The state is re-read inside: it may have changed
        between the unlocked read and the acquire, and the decision that mutates
        has to be made on what is true when it mutates.
        """
        if self.state == CircuitState.OPEN:
            raise self._open_error()

        async with self._lock:
            # Annotated so the narrowing from the check above does not follow the
            # property in here. `state` is time-dependent and another task may
            # have moved `_state`, so the re-read is deliberate, not redundant.
            current: CircuitState = self.state
            if current == CircuitState.OPEN:
                raise self._open_error()
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
                if self._state != CircuitState.HALF_OPEN:
                    self._half_open_generation += 1
                    self._last_state_change = self._opened_at + self.config.recovery_timeout
                self._half_open_calls += 1
                self._state = CircuitState.HALF_OPEN
                probe_generation: int | None = self._half_open_generation
            else:
                probe_generation = None
            self._entry_probes.set(self._entry_probes.get() + ((probe_generation, self._era),))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> Literal[False]:
        entries = self._entry_probes.get()
        if not entries:
            raise RuntimeError("Circuit breaker context exited without a matching entry")
        probe_generation, epoch = entries[-1]
        self._entry_probes.set(entries[:-1])

        if probe_generation is None:
            await self._record_closed_request_result(exc_val, epoch)
            return False

        if exc_val is None:
            await self._record_probe_success(probe_generation)
            return False

        if self._is_monitored_exception(exc_val):
            await self._record_probe_failure(exc_val, probe_generation)
        else:
            await self._release_half_open_probe(probe_generation)
        return False

    async def _record_closed_request_result(
        self, exc: BaseException | None, epoch: int | None = None
    ) -> None:
        """Record work admitted while closed without deciding a later probe's outcome.

        `epoch` is the value of `_era` when the call was admitted. A call admitted before a
        `reset()`, a `trip()` or a recovery is counted in the totals but not toward the failure
        run, so a failure that was already in flight cannot reopen a circuit that has since
        closed.
        """
        async with self._lock:
            current = epoch is None or epoch == self._era
            if exc is None:
                self._success_count += 1
                if self._state == CircuitState.CLOSED and current:
                    self._failure_count = 0
                return

            if not self._is_monitored_exception(exc):
                return

            self._last_failure = exc
            if self._state != CircuitState.CLOSED or not current:
                return
            self._count_a_closed_failure(time.monotonic())

    def _count_a_closed_failure(self, now: float) -> None:
        """One more failure while CLOSED; trip the circuit at the threshold. Holds the lock."""
        self._failure_count += 1
        if self._failure_count >= self.config.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = now
            self._last_state_change = now
            self._half_open_calls = 0

    async def _record_probe_success(self, generation: int) -> None:
        """Close the circuit only when this probe belongs to the current recovery."""
        async with self._lock:
            self._success_count += 1
            if self._state == CircuitState.HALF_OPEN and generation == self._half_open_generation:
                self._era += 1
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._half_open_calls = 0
                self._last_state_change = time.monotonic()

    async def _record_probe_failure(self, exc: BaseException, generation: int) -> None:
        """Reopen the circuit only when this probe belongs to the current recovery."""
        async with self._lock:
            self._last_failure = exc
            if self._state == CircuitState.HALF_OPEN and generation == self._half_open_generation:
                now = time.monotonic()
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._last_state_change = now
                self._half_open_calls = 0

    async def _release_half_open_probe(self, generation: int) -> None:
        """Return admission when a probe ends without a monitored outcome."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN and generation == self._half_open_generation:
                self._half_open_calls = max(0, self._half_open_calls - 1)

    async def call(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Execute an async callable guarded by the circuit breaker."""
        async with self:
            return await func(*args, **kwargs)

    async def record_success(self) -> None:
        """Record the success of a call the caller ran itself, without ``async with``.

        Nothing here says which circuit the call belonged to, so it is read as a call admitted
        while CLOSED: it counts, and clears the failure run while the circuit is CLOSED, and it
        decides nothing once the circuit has left CLOSED (a straggler's success must not close a
        circuit whose probe has not run). A probe is a call admitted through ``async with`` or
        :meth:`call`, which knows which recovery it belongs to.
        """
        await self._record_closed_request_result(None)

    async def record_failure(self, exc: BaseException | None = None) -> None:
        """Record the failure of a call the caller ran itself, without ``async with``.

        Counted unconditionally, whatever the exception (the caller decided it was a failure),
        and read like :meth:`record_success`: as a call admitted while CLOSED. While CLOSED it
        counts toward the threshold and may trip the circuit; once the circuit has left CLOSED it
        only updates ``last_failure``, because a call that started before the trip and failed
        late is not a failed probe and must not reopen the circuit or restart its cooldown.
        Without ``exc`` the failure is counted and ``last_failure`` is left as it was: a failure
        that names no exception does not erase the one before it.
        """
        async with self._lock:
            if exc is not None:
                self._last_failure = exc
            if self._state != CircuitState.CLOSED:
                return
            self._count_a_closed_failure(time.monotonic())

    def reset(self) -> None:
        """Reset the circuit breaker to CLOSED with 0 failures.

        Calls admitted before the reset do not count toward the new run of failures when they
        finish: a failure already in flight cannot reopen what this closed.
        """
        self._era += 1
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_calls = 0
        self._last_failure = None
        self._last_state_change = time.monotonic()

    def trip(self) -> None:
        """Forcefully trip the circuit breaker to OPEN."""
        self._era += 1
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

        While HALF_OPEN it is admitted without limit: it takes no probe slot, and
        ``half_open_max_calls`` bounds the awaited calls only. A fire-and-forget call has
        no outcome to close or reopen the circuit, so only awaited calls are the probes, and a
        service that fires many of these while its dependency is recovering sends all of them.
        """
        if self._circuit_breaker.state == CircuitState.OPEN:
            raise self._circuit_breaker._open_error()
        return super().call_async(**kwargs)


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
        if circuit_breaker is not None and config is not None:
            raise ValueError(
                "ResilientRpcProxy takes circuit_breaker= or config=, not both: a breaker brings "
                "its own config, so the thresholds in config= would be ignored"
            )
        super().__init__(service_name, namespace=namespace)
        self.config = config or CircuitBreakerConfig()
        self._custom_circuit_breaker = circuit_breaker

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """The one breaker every service instance shares, when one was passed in.

        Without an explicit ``circuit_breaker=`` each service instance gets its own, built when
        the instance first reads the proxy, so there is no single breaker to return from the
        class: this raises instead of returning one no call goes through, which would always
        read CLOSED. Read it from an instance (``svc.inventory.circuit_breaker``).
        """
        if self._custom_circuit_breaker is None:
            raise AttributeError(
                f"ResilientRpcProxy({self.service_name!r}) has no circuit_breaker of its own: "
                f"each service instance gets its own, so read it from an instance, "
                f"e.g. `service.<attribute>.circuit_breaker`"
            )
        return self._custom_circuit_breaker

    @overload
    def __get__(self, instance: None, owner: type | None = ...) -> ResilientRpcProxy: ...

    @overload
    def __get__(self, instance: object, owner: type | None = ...) -> ResilientServiceProxy: ...

    def __get__(
        self, instance: Any, owner: type | None = None
    ) -> ResilientServiceProxy | ResilientRpcProxy:
        if instance is None:
            return self

        def build() -> ResilientServiceProxy:
            cb = self._custom_circuit_breaker or CircuitBreaker(
                name=self.service_name, config=self.config
            )
            return ResilientServiceProxy(
                instance,
                self.service_name,
                self._namespace,
                circuit_breaker=cb,
            )

        proxy: ResilientServiceProxy = self._proxies.get_or_make(instance, build)
        return proxy

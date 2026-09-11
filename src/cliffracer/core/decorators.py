"""Service handler decorators.

Marks methods for discovery at service startup: ``@rpc`` and ``@async_rpc`` for
request-reply dispatch, ``@listener`` and ``@broadcast`` for event subscriptions,
and ``@timer`` for interval execution.
"""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from .exceptions import ConfigurationError
from .idempotency import idempotent


def _unusable_subject_reason(subject: str) -> str | None:
    """Why NATS would refuse this subject, or None if it looks usable.

    Deliberately narrow. Catches unambiguously invalid subject formats (empty
    tokens, whitespace) without preempting broker-side wildcard rules.
    """
    if not subject:
        return "it is empty"
    if any(character.isspace() for character in subject):
        return "it contains whitespace"
    if any(token == "" for token in subject.split(".")):
        return "it has an empty token (a leading, trailing or doubled '.')"
    return None


def refuse_bare_use(first: object, decorator: str, correct: str) -> None:
    """Refuse ``@factory`` where ``@factory(...)`` was meant.

    A decorator factory applied without parentheses receives the decorated
    function as its first argument and returns an inner closure without setting
    handler registration attributes. This check catches the missing call at
    decoration time with an informative ConfigurationError.
    """
    if callable(first) and not isinstance(first, type):
        raise ConfigurationError(
            f"@{decorator} is a decorator factory: write {correct}. Used bare, "
            f"it takes {getattr(first, '__name__', 'your function')!r} as its "
            f"first argument and returns the inner decorator, so nothing marks "
            f"the method and the service starts without it."
        )


def _validate_subject(subject: object, decorator: str) -> None:
    """Validate subject format at decoration time.

    Ensures the subject is a valid string rather than a model class, failing
    fast at import time before NATS connection.
    """
    if isinstance(subject, type) and issubclass(subject, BaseModel):
        raise ConfigurationError(
            f"@{decorator} takes a NATS subject string, not the model class "
            f"{subject.__name__!r}. Passing a model is the right instinct for "
            f"validation -- @validated_listener(subject, Model) is the decorator "
            f"that takes both."
        )

    if not isinstance(subject, str):
        raise ConfigurationError(
            f"@{decorator} takes a NATS subject string, not "
            f"{type(subject).__name__}. For a model-validated handler use "
            f"@validated_listener(subject, Model)."
        )

    reason = _unusable_subject_reason(subject)
    if reason is not None:
        raise ConfigurationError(
            f"@{decorator} was given {subject!r}, which NATS will refuse: "
            f"{reason}. Left to the broker this surfaces as "
            f"'nats: invalid subject' when the service connects, naming neither "
            f"the handler nor the decorator that caused it."
        )


def rpc(func: Any) -> Any:
    """
    Decorator to mark a method as an RPC handler.

    The method will be exposed as {service_name}.rpc.{method_name}
    """
    func._cliffracer_rpc = True
    return func


def async_rpc(func: Any) -> Any:
    """
    Decorator to mark a method as an async RPC handler.

    Same as @rpc but emphasizes async nature for clarity.
    """
    func._cliffracer_rpc = True
    func._cliffracer_async_rpc = True
    return func


def listener(
    pattern: str,
    cross_namespace: bool = False,
    durable: str | None = None,
    fanout: bool = False,
    pull: bool = False,
) -> Callable:
    """
    Decorator to mark a method as an event listener.

    Args:
        pattern: NATS subject pattern to listen for (supports wildcards).
        cross_namespace: if True, subscribe across all namespaces (*.{pattern}).
        durable: JetStream durable consumer name. Ignored unless the service
            sets ``jetstream_enabled``. When set, the listener binds a durable
            push consumer with explicit ack instead of a core subscription, so
            a restarting service is redelivered anything it had not acked.

            The name comes from you and is deliberately NOT derived from the
            service name: two replicas of the same service must share one
            consumer, and a per-instance name would process every message twice.
        fanout: declare that every replica should handle every message. A
            listener with no durable has no queue group and broadcasts to all
            replicas. A listener with neither durable nor fanout is refused at
            startup to prevent accidental broadcast.
        pull: bind the durable as a PULL consumer instead of a push one. The
            replica fetches when it has capacity, providing per-replica
            backpressure bounded by max_ack_pending.

            Requires ``durable`` and ``jetstream_enabled``. NOT an in-place
            change to an existing durable: a consumer's config is fixed at
            creation and nats-py adopts an existing one wholesale, so switching
            needs a NEW durable name or ``nats consumer rm <STREAM> <durable>``
            as a deliberate deploy step.

    Example:
        @listener("user.events.*", fanout=True)
        async def handle_user_event(self, subject: str, **data):
            logger.info(f"User event: {subject}")

        @listener("events.extraction.requested", durable="pdf-extractor")
        async def on_request(self, subject: str, **data):
            ...
    """

    _validate_subject(pattern, "listener")

    def decorator(func: Any) -> Any:
        if not hasattr(func, "_cliffracer_events"):
            func._cliffracer_events = []
        func._cliffracer_events.append(pattern)
        if cross_namespace:
            if not hasattr(func, "_cliffracer_event_cross_namespace"):
                func._cliffracer_event_cross_namespace = set()
            func._cliffracer_event_cross_namespace.add(pattern)
        if durable:
            if not hasattr(func, "_cliffracer_event_durables"):
                func._cliffracer_event_durables = {}
            func._cliffracer_event_durables[pattern] = durable
        if fanout:
            if not hasattr(func, "_cliffracer_event_fanout"):
                func._cliffracer_event_fanout = set()
            func._cliffracer_event_fanout.add(pattern)
        if pull:
            if not hasattr(func, "_cliffracer_event_pull"):
                func._cliffracer_event_pull = set()
            func._cliffracer_event_pull.add(pattern)
        return func

    return decorator


def validated_listener(
    pattern: str,
    schema: type[BaseModel],
    on_invalid: str | None = None,
    cross_namespace: bool = False,
    durable: str | None = None,
    fanout: bool = False,
) -> Callable:
    """
    Decorator to mark a method as a schema-validated event listener.

    The incoming event payload is validated against ``schema`` before the handler
    runs; the handler receives the validated model as ``message``. Invalid messages
    are handled per ``on_invalid`` ("deadletter" or "drop"); ``None`` uses the
    service's ``ServiceConfig.default_on_invalid``.

    Args:
        pattern: NATS subject pattern to listen for (supports wildcards).
        schema: Pydantic model the payload is validated against.
        on_invalid: "deadletter" | "drop" | None (use service default).
        cross_namespace: if True, subscribe across all namespaces (*.{pattern}).
        durable: JetStream durable consumer name. Ignored unless the service
            sets ``jetstream_enabled``. When set, the listener binds a durable
            push consumer with explicit ack instead of a core subscription, so
            a restarting service is redelivered anything it had not acked.

            The name comes from you and is deliberately NOT derived from the
            service name: two replicas of the same service must share one
            consumer, and a per-instance name would process every message twice.

        fanout: declare that every replica should handle every message; see
            ``listener``. A listener with neither durable nor fanout is
            refused at startup to require an explicit choice between broadcast
            and queue-group delivery.

    Example:
        @validated_listener("orders.created", OrderCreated, fanout=True)
        async def on_order(self, message: OrderCreated):
            ...
    """

    _validate_subject(pattern, "validated_listener")

    def decorator(func: Any) -> Any:
        if not hasattr(func, "_cliffracer_validated_events"):
            func._cliffracer_validated_events = []
        func._cliffracer_validated_events.append((pattern, schema, on_invalid))
        if cross_namespace:
            if not hasattr(func, "_cliffracer_event_cross_namespace"):
                func._cliffracer_event_cross_namespace = set()
            func._cliffracer_event_cross_namespace.add(pattern)
        if durable:
            if not hasattr(func, "_cliffracer_event_durables"):
                func._cliffracer_event_durables = {}
            func._cliffracer_event_durables[pattern] = durable
        if fanout:
            if not hasattr(func, "_cliffracer_event_fanout"):
                func._cliffracer_event_fanout = set()
            func._cliffracer_event_fanout.add(pattern)
        return func

    return decorator


def broadcast(pattern: str) -> Callable[..., Any]:
    """
    Decorator to mark a method as a broadcast handler.

    Args:
        pattern: Message pattern to handle

    Example:
        @broadcast("system.alerts")
        async def handle_alert(self, **data):
            await self.broadcast_to_websockets(data)
    """

    _validate_subject(pattern, "broadcast")

    def decorator(func: Any) -> Any:
        func._cliffracer_broadcast = pattern
        return func

    return decorator


def timer(interval: float, eager: bool = False, **kwargs: Any) -> Callable[..., Any]:
    """
    Decorator for creating timer-triggered methods.

    Args:
        interval: Time in seconds between executions
        eager: If True, execute immediately on service start
        **kwargs: Additional timer configuration options

    Example:
        @timer(interval=30)
        async def health_check(self):
            await self.check_database_connection()

        @timer(interval=60, eager=True)
        async def cleanup_cache(self):
            await self.remove_expired_entries()
    """

    refuse_bare_use(interval, "timer", "@timer(interval=60)")

    def decorator(func: Any) -> Any:
        from .timer import Timer

        timer_instance = Timer(interval=interval, eager=eager, **kwargs)
        return timer_instance(func)

    return decorator


# Composition helpers
# Convenience decorator combinations

__all__ = [
    "rpc",
    "async_rpc",
    "listener",
    "validated_listener",
    "broadcast",
    "timer",
    "idempotent",
    "refuse_bare_use",
]

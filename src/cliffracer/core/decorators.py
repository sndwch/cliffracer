"""Service handler decorators.

Marks methods for discovery at service startup: ``@rpc`` and ``@async_rpc`` for
request-reply dispatch, ``@listener`` and ``@broadcast`` for event subscriptions,
and ``@timer`` for interval execution.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

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


_ON_INVALID_STRATEGIES = ("deadletter", "drop")


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


#: The longest JetStream consumer name the broker accepts.
_MAX_DURABLE_NAME = 255
_DURABLE_FORBIDDEN = {".": "'.'", "*": "'*'", ">": "'>'", "/": "'/'", "\\": "'\\'"}


def _validate_durable(durable: object, decorator: str, handler: Any) -> None:
    """Refuse a durable consumer name the broker would refuse, naming the handler.

    A name travels unchanged from the decorator to `_setup_subscriptions`, where a broker refusal
    arrives after the connection is up and names neither the handler nor the decorator. The rule
    is the one a broker was measured to enforce: no `.`, `*`, `>`, `/`, `\\` or whitespace, at
    most 255 characters. An empty or absent durable is no durable.
    """
    if durable is None or durable == "":
        return
    where = f"@{decorator} on {getattr(handler, '__qualname__', handler)}"
    if not isinstance(durable, str):
        raise ConfigurationError(
            f"{where} was given durable={durable!r}, a {type(durable).__name__}; a durable "
            f"consumer name is a str."
        )
    fault: str | None = None
    for character in durable:
        if character in _DURABLE_FORBIDDEN:
            fault = f"contains {_DURABLE_FORBIDDEN[character]}"
            break
        if character.isspace():
            fault = "contains whitespace"
            break
    if fault is None and len(durable) > _MAX_DURABLE_NAME:
        fault = f"is {len(durable)} characters, and the most a consumer name may be is {_MAX_DURABLE_NAME}"
    if fault is not None:
        raise ConfigurationError(
            f"{where} was given durable={durable!r}, which JetStream will refuse as a consumer "
            f"name: it {fault}. Left to the broker this surfaces as 'invalid consumer name' "
            f"when the service connects, naming neither the handler nor the decorator."
        )


def _declare_max_concurrency(func: Any, limit: object, decorator: str) -> None:
    """Record `limit` as the most calls of `func` that run at once, refusing one that cannot be.

    One limit per method: a second decorator on the same method may repeat it, not change it.
    """
    if limit is None:
        return
    where = f"@{decorator} on {getattr(func, '__qualname__', func)}"
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ConfigurationError(
            f"{where} was given max_concurrency={limit!r}; it is the most calls of the handler "
            f"that run at once, a positive int."
        )
    declared = getattr(func, "_cliffracer_max_concurrency", None)
    if declared is not None and declared != limit:
        raise ConfigurationError(
            f"{where} was given max_concurrency={limit}, and another decorator on the same "
            f"method gave {declared}. A method has one limit, across all its subjects."
        )
    func._cliffracer_max_concurrency = limit


def _declare_max_queued(func: Any, queued: object, decorator: str) -> None:
    """Record `queued` as the most requests that may wait at `func` when it is full.

    It bounds a queue, so it needs `max_concurrency`; and one method has one, as it has one limit.
    """
    if queued is None:
        return
    where = f"@{decorator} on {getattr(func, '__qualname__', func)}"
    if isinstance(queued, bool) or not isinstance(queued, int) or queued < 0:
        raise ConfigurationError(
            f"{where} was given max_queued={queued!r}; it is the most requests that wait at the "
            f"method when it is full, an int of 0 or more."
        )
    if getattr(func, "_cliffracer_max_concurrency", None) is None:
        raise ConfigurationError(
            f"{where} was given max_queued={queued} without max_concurrency: a method with no "
            f"concurrency limit is never full, so nothing waits at it."
        )
    declared = getattr(func, "_cliffracer_max_queued", None)
    if declared is not None and declared != queued:
        raise ConfigurationError(
            f"{where} was given max_queued={queued}, and another decorator on the same method "
            f"gave {declared}. A method has one queue, across its rpc and async subjects."
        )
    func._cliffracer_max_queued = queued


def _refuse_max_queued(queued: object, decorator: str) -> None:
    """Refuse `max_queued` on a listener: it bounds the requests a method admits, and an event
    takes no admission slot (a core listener's wait holds only its own pattern's messages, and a
    JetStream message waits on the broker)."""
    if queued is not None:
        raise ConfigurationError(
            f"@{decorator} was given max_queued={queued!r}; max_queued applies to requests "
            f"(@rpc, @async_rpc), which take admission slots, and an event takes none."
        )


def _rpc_marker(
    given: Any,
    max_concurrency: int | None,
    decorator: str,
    *,
    is_async: bool,
    max_queued: int | None = None,
) -> Any:
    """`@rpc` and `@async_rpc`, used bare or as `(max_concurrency=n, max_queued=q)`."""

    def mark(func: Any) -> Any:
        _declare_max_concurrency(func, max_concurrency, decorator)
        _declare_max_queued(func, max_queued, decorator)
        func._cliffracer_rpc = True
        if is_async:
            func._cliffracer_async_rpc = True
        return func

    if given is None:
        return mark
    if not callable(given):
        raise ConfigurationError(
            f"@{decorator} was given {given!r} where the handler goes. A concurrency limit is "
            f"a keyword: @{decorator}(max_concurrency={given!r})."
        )
    return mark(given)


def rpc(
    func: Any = None, /, *, max_concurrency: int | None = None, max_queued: int | None = None
) -> Any:
    """
    Decorator to mark a method as an RPC handler.

    The method will be exposed as {service_name}.rpc.{method_name}, with the service's
    namespace in front when `ServiceConfig.namespace` is set:
    {namespace}.{service_name}.rpc.{method_name}. It is also reachable, fire-and-forget,
    on {service_name}.async.{method_name} (namespaced the same way), whichever
    decorator marks it.

    `@rpc(max_concurrency=n)` runs at most `n` calls of the method at once, across both
    subjects. A request over it waits for the method's permit before the service's, bounded by
    its deadline; `@rpc` alone sets no limit.

    `max_queued=q` (with `max_concurrency`) is the most requests that may wait at the method when
    it is full; one past it is answered `busy` without taking an admission slot. Unset, it is half
    of `max_rpc_in_flight` (at least 1) when the service sets that bound, and no bound otherwise.
    """
    return _rpc_marker(func, max_concurrency, "rpc", is_async=False, max_queued=max_queued)


def async_rpc(
    func: Any = None, /, *, max_concurrency: int | None = None, max_queued: int | None = None
) -> Any:
    """
    Decorator to mark a method as an async RPC handler.

    Identical to @rpc in what the framework does with it: it is registered as an RPC
    handler and reachable on both the `rpc` and the `async` subjects, and the extra
    marker it sets is not read. It records, for the reader, that the method is meant to
    be called with `call_async`. `max_concurrency` and `max_queued` are @rpc's.
    """
    return _rpc_marker(func, max_concurrency, "async_rpc", is_async=True, max_queued=max_queued)


def _pause_when_down_names(names: object, decorator: str, handler: Any) -> tuple[str, ...]:
    """The dependency names a listener pauses on, checked as far as a decorator can check them.

    A bare string is refused rather than read as one name per character, and so is a name that is
    not a non-empty string or that appears twice. Whether each name is a declared dependency, and
    whether the listener is durable, is checked at discovery, where both are known.
    """
    where = f"@{decorator} on {getattr(handler, '__qualname__', handler)}"
    if isinstance(names, str):
        raise ConfigurationError(
            f"{where} was given pause_when_down={names!r}, a str; it takes a tuple of "
            f"dependency names: pause_when_down=({names!r},)."
        )
    if not isinstance(names, tuple | list | frozenset | set):
        raise ConfigurationError(
            f"{where} was given pause_when_down={names!r}; it takes a tuple of dependency names."
        )
    ordered = tuple(names)
    for name in ordered:
        if not isinstance(name, str) or not name:
            raise ConfigurationError(
                f"{where} was given pause_when_down={names!r}: {name!r} is not a dependency "
                f"name, which is a non-empty str."
            )
    if len(set(ordered)) != len(ordered):
        raise ConfigurationError(
            f"{where} was given pause_when_down={names!r}, which names a dependency twice."
        )
    return ordered


def listener(
    pattern: str,
    cross_namespace: bool = False,
    durable: str | None = None,
    fanout: bool = False,
    pull: bool = False,
    pause_when_down: tuple[str, ...] = (),
    max_concurrency: int | None = None,
    max_queued: int | None = None,
) -> Callable:
    """
    Decorator to mark a method as an event listener.

    Args:
        pattern: NATS subject pattern to listen for (supports wildcards).
        cross_namespace: if True, subscribe across all namespaces (*.{pattern}). The service
            must have a namespace; discovery refuses it otherwise.
        durable: JetStream durable consumer name: no ``.``, ``*``, ``>``, ``/``, ``\\`` or
            whitespace, at most 255 characters, refused when the handler is declared. Requires the service to set
            ``jetstream_enabled``: without it a listener declaring a durable is
            refused at startup. The one exception is a listener that also sets
            ``fanout=True``, which starts with the durable inert -- and is
            refused once ``jetstream_enabled`` is set, because a durable and
            fanout mean opposite things. When set, the listener binds a durable
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
        pause_when_down: names of declared dependencies (``@dependency`` or
            ``add_dependency``). While any of them is down, this replica stops
            consuming the listener's durable and starts again when they are all
            up, so messages wait in the stream instead of being redelivered
            into the dead-letter subject. Requires ``durable`` and
            ``jetstream_enabled``; see "Pausing a listener while a dependency is
            down" in the api reference.
        max_concurrency: the most messages this method handles at once, across all its
            patterns and any ``@rpc`` on it. A message over it waits for the method's permit
            before the service's ``max_event_concurrency`` permit. Unset is no limit.
        max_queued: refused: it bounds the requests a method admits, and an event takes no
            admission slot.

    Every parameter after ``subject`` is a field of the event payload and must
    be annotated; ``**kwargs`` is refused at startup.

    Example:
        @listener("user.events.*", fanout=True)
        async def handle_user_event(self, subject: str, user_id: str) -> None:
            logger.info(f"User event {subject} for {user_id}")

        @listener("events.extraction.requested", durable="pdf-extractor")
        async def on_request(self, subject: str, document_id: str) -> None:
            ...
    """

    _validate_subject(pattern, "listener")
    _refuse_max_queued(max_queued, "listener")

    def decorator(func: Any) -> Any:
        if not hasattr(func, "_cliffracer_events"):
            func._cliffracer_events = []
        func._cliffracer_events.append(pattern)
        if cross_namespace:
            if not hasattr(func, "_cliffracer_event_cross_namespace"):
                func._cliffracer_event_cross_namespace = set()
            func._cliffracer_event_cross_namespace.add(pattern)
        _validate_durable(durable, "listener", func)
        _declare_max_concurrency(func, max_concurrency, "listener")
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
        names = _pause_when_down_names(pause_when_down, "listener", func)
        if names:
            if not hasattr(func, "_cliffracer_event_pause_when_down"):
                func._cliffracer_event_pause_when_down = {}
            func._cliffracer_event_pause_when_down[pattern] = names
        return func

    return decorator


def validated_listener(
    pattern: str,
    schema: type[BaseModel],
    on_invalid: Literal["deadletter", "drop"] | None = None,
    cross_namespace: bool = False,
    durable: str | None = None,
    fanout: bool = False,
    pause_when_down: tuple[str, ...] = (),
    max_concurrency: int | None = None,
    max_queued: int | None = None,
) -> Callable:
    """
    Decorator to mark a method as a schema-validated event listener.

    The incoming event payload is validated against ``schema`` before the handler
    runs; the handler receives the validated model through its sole payload
    parameter. Invalid messages are handled per ``on_invalid`` ("deadletter" or
    "drop"); ``None`` uses the service's ``ServiceConfig.default_on_invalid``.

    It takes no ``pull`` option: a schema-validated listener is always a push listener, and
    declaring ``listener(..., pull=True)`` and this decorator on one subject is refused as a
    duplicate listener. A handler that needs a pull consumer is a plain ``listener``.

    Args:
        pattern: NATS subject pattern to listen for (supports wildcards).
        schema: Pydantic model the payload is validated against.
        on_invalid: "deadletter" | "drop" | None (use service default). Any
            other value is refused when the decorator runs, because dispatch
            dead-letters only on exactly "deadletter".
        cross_namespace: if True, subscribe across all namespaces (*.{pattern}). The service
            must have a namespace; discovery refuses it otherwise.
        durable: JetStream durable consumer name: no ``.``, ``*``, ``>``, ``/``, ``\\`` or
            whitespace, at most 255 characters, refused when the handler is declared. Requires the service to set
            ``jetstream_enabled``: without it a listener declaring a durable is
            refused at startup. The one exception is a listener that also sets
            ``fanout=True``, which starts with the durable inert -- and is
            refused once ``jetstream_enabled`` is set, because a durable and
            fanout mean opposite things. When set, the listener binds a durable
            push consumer with explicit ack instead of a core subscription, so
            a restarting service is redelivered anything it had not acked.

            The name comes from you and is deliberately NOT derived from the
            service name: two replicas of the same service must share one
            consumer, and a per-instance name would process every message twice.

        fanout: declare that every replica should handle every message; see
            ``listener``. A listener with neither durable nor fanout is
            refused at startup to require an explicit choice between broadcast
            and queue-group delivery.
        pause_when_down: names of declared dependencies to stop consuming
            while any of them is down; see ``listener``.
        max_concurrency: the method's concurrency limit; see ``listener``.

    Example:
        @validated_listener("orders.created", OrderCreated, fanout=True)
        async def on_order(self, message: OrderCreated):
            ...
    """

    _validate_subject(pattern, "validated_listener")
    _refuse_max_queued(max_queued, "validated_listener")
    if on_invalid is not None and on_invalid not in _ON_INVALID_STRATEGIES:
        raise ConfigurationError(
            f"@validated_listener on_invalid={on_invalid!r} is not a strategy. Use "
            f"'deadletter' or 'drop', or leave it unset to use the service's "
            f"default_on_invalid. Dispatch dead-letters only on exactly "
            f"'deadletter', so any other value would drop every invalid message."
        )

    def decorator(func: Any) -> Any:
        if not hasattr(func, "_cliffracer_validated_events"):
            func._cliffracer_validated_events = []
        func._cliffracer_validated_events.append((pattern, schema, on_invalid))
        if cross_namespace:
            if not hasattr(func, "_cliffracer_event_cross_namespace"):
                func._cliffracer_event_cross_namespace = set()
            func._cliffracer_event_cross_namespace.add(pattern)
        _validate_durable(durable, "validated_listener", func)
        _declare_max_concurrency(func, max_concurrency, "validated_listener")
        if durable:
            if not hasattr(func, "_cliffracer_event_durables"):
                func._cliffracer_event_durables = {}
            func._cliffracer_event_durables[pattern] = durable
        if fanout:
            if not hasattr(func, "_cliffracer_event_fanout"):
                func._cliffracer_event_fanout = set()
            func._cliffracer_event_fanout.add(pattern)
        names = _pause_when_down_names(pause_when_down, "validated_listener", func)
        if names:
            if not hasattr(func, "_cliffracer_event_pause_when_down"):
                func._cliffracer_event_pause_when_down = {}
            func._cliffracer_event_pause_when_down[pattern] = names
        return func

    return decorator


def broadcast(
    pattern: str, *, max_concurrency: int | None = None, max_queued: int | None = None
) -> Callable[..., Any]:
    """
    Decorator to mark a method as a broadcast handler.

    A broadcast handler is an event listener that every replica handles, as
    ``@listener(pattern, fanout=True)`` is. Its parameters follow the same rule:
    every one after ``subject`` is an annotated payload field.

    Args:
        pattern: Message pattern to handle
        max_concurrency: the method's concurrency limit; see ``listener``.

    Example:
        @broadcast("system.alerts")
        async def handle_alert(self, subject: str, level: str, message: str) -> None:
            self.logger.warning(f"{level}: {message}")
    """

    _validate_subject(pattern, "broadcast")

    _refuse_max_queued(max_queued, "broadcast")

    def decorator(func: Any) -> Any:
        _declare_max_concurrency(func, max_concurrency, "broadcast")
        func._cliffracer_broadcast = pattern
        return func

    return decorator


def timer(
    interval: float,
    eager: bool = False,
    headers: dict[str, str] | None = None,
    token_factory: Callable[[], str | Awaitable[str]] | None = None,
    **kwargs: Any,
) -> Callable[..., Any]:
    """
    Decorator for creating timer-triggered methods.

    The same decorator as `cliffracer.core.timer.timer`, which builds it.

    Args:
        interval: Time in seconds between executions
        eager: If True, execute immediately on service start
        headers: Optional headers passed in WorkerContext
        token_factory: Optional callable returning a bearer token, or an awaitable of one
            (an `async def` factory), which a timer dispatch sends as its `authorization`
            header. A service whose
            `AuthExtension` has `allow_timers=False` refuses a timer without one. The
            extension reads a timer's token from that header whichever header it reads
            from messages.
        **kwargs: Additional timer configuration options

    Example:
        @timer(interval=30)
        async def check_database(self):
            await self.check_database_connection()

        @timer(interval=60, eager=True)
        async def cleanup_cache(self):
            await self.remove_expired_entries()
    """

    # One implementation, so the two spellings cannot drift apart.
    from .timer import timer as build_timer

    return build_timer(
        interval, eager=eager, headers=headers, token_factory=token_factory, **kwargs
    )


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

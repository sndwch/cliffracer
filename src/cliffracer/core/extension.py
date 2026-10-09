"""The extension contract.

An extension is an object declared as a class attribute on a service, bound
per service instance, and run by the container. It never participates in the
MRO: order is declaration order, and every hook is optional.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import decimal
import fractions
import functools
import inspect
import math
import pathlib
import uuid
import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeGuard

from loguru import logger
from pydantic import BaseModel

from cliffracer.core.exceptions import CliffracerError

if TYPE_CHECKING:
    from cliffracer.core.service_config import ServiceConfig


#: Keys the health and info payloads carry themselves. An extension's contribution is
#: published under its own name, at the top level, so an extension named one of these
#: would replace it: `status` pins /health at 503 and `name` replaces the service's name.
#: Names beginning with an underscore are never published, so they cannot collide.
RESERVED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        # health
        "service",
        "status",
        "timestamp",
        "nats_connected",
        "nats_rtt_ms",
        "broker_state",
        "features",
        "dead_letters_lost",
        "dependencies",
        "unhealthy_dependencies",
        "dependencies_error",
        "details_error",
        # info
        "name",
        "version",
        "rpc_methods",
        "event_patterns",
        "timer_methods",
        "subjects",
        # info, added by the listener after `get_service_info()` returns
        "health_port",
    }
)


class ExtensionIsolationError(CliffracerError):
    """Raised when an extension specification argument cannot be safely isolated across service instances."""


class SharedDependency[T]:
    """Explicit opt-in wrapper allowing state to be shared across service instances."""

    def __init__(self, value: T) -> None:
        self.value = value

    def unwrap(self) -> T:
        return self.value

    def __repr__(self) -> str:
        return f"SharedDependency({self.value!r})"


@dataclass
class ExtensionSetupContext:
    service_config: ServiceConfig
    broker_url: str
    service: Any

    def __getattr__(self, name: str) -> Any:
        # Only reached for a name this object does not have, and everything it
        # delegates is the service's. `service` itself is never delegated: on an
        # instance that has not been initialised yet (what `copy` and `pickle`
        # build before they restore the fields) looking it up would come back here
        # and recurse for ever. The protocol names (`__deepcopy__`, `__setstate__`)
        # belong to this object, not to the service behind it.
        if name == "service" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self.service, name)


@dataclass
class WorkerContext:
    """What one dispatch knows about itself, shared by every hook on the chain."""

    kind: str
    subject: str | None
    headers: dict[str, str]
    correlation_id: str | None
    # The decoded wire payload, as it arrived: not coerced to the handler's parameter
    # types and with `correlation_id` still in it. An RPC handler's validated arguments
    # are in `data["validated_kwargs"]`. It is a dict for any sender that follows the
    # framework's envelope; a sender that encodes something else puts that here.
    payload: dict[str, Any]
    raw: Any = None
    data: dict[str, Any] = field(default_factory=dict)


class RejectMessage(Exception):
    """Refuse a message. From an extension, honoured ONLY from worker_setup.

    THE ONE EXCEPTION TO HOOK ISOLATION. Every other hook exception is logged under
    its extension's name and cannot change the handler's outcome -- that is what
    stops a buggy metrics hook taking down dispatch, and it stays true. But a
    check that cannot refuse is not a check: an AuthExtension checks a token
    header on the hook chain, and without a rejection channel an unauthenticated
    message would reach the handler and receive a normal reply.

    Raised from worker_setup, the container skips the handler, runs
    worker_result with this as `exc`, runs worker_teardown, and answers the
    caller on its own path. Raised from any other hook it is swallowed like
    anything else: by then the handler has already run and refusing is a lie.
    Raised by the HANDLER body it is honoured too: the caller gets the refusal and
    a durable message is acknowledged rather than redelivered.

    The extensions declared AFTER one that refuses never had `worker_setup`
    called but still get `worker_result` and `worker_teardown`.

    `hook_crash` marks the one kind of RejectMessage no extension authored: the
    pipeline synthesises one when a `fails_closed` hook RAISES, because the
    handler must not run. That is the service being broken, not the caller being
    turned away, and the two route to different people -- so the wire reports it
    as a fault. It is a keyword rather than a subclass, so a handler of
    `RejectMessage` sees one type for every refusal.

    It is set at the raise site rather than read from `__cause__` at the
    boundary. `__cause__ is None` is the absence of a declaration, not a
    declaration: an extension writing `raise RejectMessage("unauthenticated")
    from token_error` is ordinary Python, and inferring from it would report a
    genuine refusal as a service fault -- the mirror of the bug this fixes, and
    the worse direction, because it buries an authorisation decision.

    `reason` REACHES THE CALLER VERBATIM, and deliberately outside
    `expose_internal_errors`: a refusal is an answer to whoever sent the
    message, not an internal error, so gating it would leave a caller unable to
    learn why they were turned away. The consequence is that an extension
    writing `raise RejectMessage(f"auth failed: {exc}")` publishes `exc`. Write
    reasons for the caller. The synthesised `hook_crash` refusal is the
    exception: that text is the service's own failure and goes through the same
    gate a handler exception does.
    """

    # The default every arm reads, so a subclass that does not call `super().__init__` is an
    # authored refusal and not an `AttributeError` out of the dispatch.
    hook_crash: bool = False

    def __init__(self, reason: str, *, hook_crash: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.hook_crash = hook_crash


def is_a_finite_delay(value: Any) -> TypeGuard[float]:
    """Whether `value` is a number of seconds that can be written down and waited: a finite
    `int` or `float`, and not a `bool`.

    `nan` and `inf` are numbers and neither can be waited, and neither is JSON: `NaN` and `Infinity`
    are not read by a client in another language. `RetryMessage.retry_after` is checked by this
    wherever it leaves the process, as a NAK delay and in a reply, so the two cannot disagree about
    which values are numbers.
    """
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


class RetryMessage(RejectMessage):
    """Refuse the current attempt while asking a durable transport to retry.

    Request/reply callers receive the same refusal as ``RejectMessage``. A
    JetStream consumer NAKs the message after ``retry_after`` seconds instead
    of acknowledging it, and dead-letters it if the consumer's delivery limit
    is already exhausted.
    """

    # The default the JetStream arm reads, as `hook_crash` is for every arm: a subclass that does
    # not call `super().__init__` asks for the configured backoff.
    retry_after: float | None = None

    def __init__(self, reason: str, *, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


def _takes_no_arguments(arg: Any) -> bool:
    """Whether *arg* can be called with no arguments, asked rather than tried.

    The old test was to CALL it and treat `TypeError` as "not a factory". That
    conflates two different things: a callable whose signature needs arguments,
    and a zero-argument factory whose body raises `TypeError` -- a very ordinary
    bug, which was then silently reported as "not a factory" and the raw
    callable deep-copied in its place.

    A callable whose signature cannot be read -- some C builtins -- is treated
    as NOT a factory, so it is copied rather than invoked. That is the
    conservative direction: copying something that was meant as a factory is
    visible at the first use, while calling something that was not is a side
    effect at import time. A C callable with no signature of its own can also be
    reported as exactly `(*args, **kwargs)` (on Python 3.12, `operator.itemgetter`,
    `attrgetter`, `methodcaller` and `sqlite3.Connection` are), which binds no
    arguments and is no reading at all: with no Python code behind it, that
    signature counts as unreadable too.
    """
    try:
        signature = inspect.signature(arg)
        signature.bind()
    except (TypeError, ValueError):
        return False
    return not (_is_only_varargs(signature) and not _has_python_code(arg))


def _is_only_varargs(signature: inspect.Signature) -> bool:
    """Whether `signature` is exactly `(*args, **kwargs)`, whatever the two are named."""
    return [parameter.kind for parameter in signature.parameters.values()] == [
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    ]


def _has_python_code(arg: Any) -> bool:
    """Whether calling `arg` runs Python code: a function, a method, a lambda, an instance whose
    class defines `__call__` in Python, a class, or a `functools.partial` of any of these."""
    while isinstance(arg, functools.partial):
        arg = arg.func
    if isinstance(arg, type) or hasattr(arg, "__code__"):
        return True
    return hasattr(inspect.getattr_static(type(arg), "__call__", None), "__code__")


# What is copied for each bound instance without a word: plain data. Any other object that
# `copy.deepcopy` clones is copied with a `FutureWarning`, because a future release will refuse it
# unless it is wrapped in `SharedDependency` or given as a zero-argument callable. A scalar or a
# class that `deepcopy` returns as it is was never cloned and is not in question.
_PLAIN_DATA_TYPES: tuple[type, ...] = (
    str,
    bytes,
    bytearray,
    int,
    float,
    complex,
    bool,
    type(None),
    list,
    dict,
    set,
    frozenset,
    tuple,
    BaseModel,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    decimal.Decimal,
    fractions.Fraction,
    uuid.UUID,
    pathlib.PurePath,
    range,
)


def _is_plain_data(arg: Any) -> bool:
    if isinstance(arg, _PLAIN_DATA_TYPES):
        return True
    return dataclasses.is_dataclass(arg) and not isinstance(arg, type)


def _warn_that_a_copy_will_be_refused(arg: Any, where: str) -> None:
    kind = type(arg)
    qualified = f"{kind.__module__}.{kind.__qualname__}"
    warnings.warn(
        f"{where}: an object of type {qualified} is copied for each service instance, because "
        f"an extension argument is deep-copied unless it is plain data. A future release will "
        f"copy only plain data (lists, dicts, sets, tuples, pydantic models, dataclasses and "
        f"value types such as numbers, strings, dates and paths) and refuse any other object. "
        f"Wrap it in SharedDependency(...) to share one object across services, or pass a "
        f"zero-argument callable to build one for each.",
        FutureWarning,
        stacklevel=3,
    )


def _a_tuple_of_its_own_type(arg: tuple[Any, ...], items: list[Any]) -> tuple[Any, ...] | None:
    """`items` held in a new tuple of `arg`'s own type, or None when that type cannot be built.

    A `NamedTuple` is built with `_make`; another subclass is built from the items as one iterable,
    or from the items as its arguments. A result counts only when it holds exactly `items`, so a
    constructor that adds, drops or converts an item is not taken for a rebuild.
    """
    kind = type(arg)
    builders: list[Any] = []
    if hasattr(kind, "_make"):
        builders.append(kind._make)
    builders.extend((lambda values: kind(values), lambda values: kind(*values)))
    for build in builders:
        try:
            rebuilt = build(items)
        except Exception:
            continue
        if len(rebuilt) == len(items) and all(a is b for a, b in zip(rebuilt, items, strict=True)):
            return rebuilt  # type: ignore[no-any-return]
    return None


def _holds_a_declaration(items: Iterable[Any]) -> bool:
    """Whether an item is one that is shared, called or built, not copied: what a whole copy loses."""
    return any(
        isinstance(item, SharedDependency | Extension)
        or (callable(item) and not isinstance(item, type) and _takes_no_arguments(item))
        for item in items
    )


def _safe_clone_arg(
    arg: Any, _memo: dict[int, Any] | None = None, where: str = "an extension argument"
) -> Any:
    """Isolate one extension argument per bound instance.

    A `SharedDependency` is unwrapped and shared; a zero-argument callable is called and
    its product used; a nested `Extension` is instantiated; a tuple, a list and a dict are
    isolated item by item (a dict's keys are left as they are), at any depth; every other
    object is deep-copied. A tuple subclass is isolated item by item and keeps its type;
    a subclass of list or dict is one of those other objects.

    The containers share one memo with the deep copies, so an object that appears twice in an
    argument is one copy, and an argument that contains itself is cloned rather than followed
    for ever, as `copy.deepcopy` does.

    Raises ExtensionIsolationError if an argument cannot be isolated, unless explicitly
    wrapped in SharedDependency.
    """
    memo: dict[int, Any] = {} if _memo is None else _memo
    if isinstance(arg, SharedDependency):
        return arg.value
    if callable(arg) and not isinstance(arg, type) and _takes_no_arguments(arg):
        try:
            return arg()
        except Exception as exc:
            # NOT swallowed. `except Exception: pass` ran the factory's side
            # effects, discarded what it raised, and then deep-copied the raw
            # callable instead -- so a broken factory produced a service that
            # looked bound and held the function where its product should be.
            # ADR-0005 says this fails loudly; this is the half that makes that
            # true.
            raise ExtensionIsolationError(
                f"Extension argument factory {getattr(arg, '__name__', arg)!r} raised "
                f"{type(exc).__name__}: {exc}. A zero-argument callable is called once per "
                f"bound instance; wrap it with SharedDependency(...) to pass the callable "
                f"itself."
            ) from exc
    if isinstance(arg, Extension):
        return arg.create_instance(None, "")
    if type(arg) is tuple:
        return tuple(_safe_clone_arg(item, memo, where) for item in arg)
    if type(arg) is list:
        if id(arg) in memo:
            return memo[id(arg)]
        cloned_list: list[Any] = []
        memo[id(arg)] = cloned_list
        cloned_list.extend(_safe_clone_arg(item, memo, where) for item in arg)
        return cloned_list
    if type(arg) is dict:
        if id(arg) in memo:
            return memo[id(arg)]
        cloned_dict: dict[Any, Any] = {}
        memo[id(arg)] = cloned_dict
        for key, value in arg.items():
            cloned_dict[key] = _safe_clone_arg(value, memo, where)
        return cloned_dict
    if isinstance(arg, tuple):
        # A tuple subclass keeps its type and its items are isolated one by one, as a plain
        # tuple's are, so a `SharedDependency` inside a `NamedTuple` is still the shared object.
        rebuilt = _a_tuple_of_its_own_type(
            arg, [_safe_clone_arg(item, memo, where) for item in arg]
        )
        if rebuilt is not None:
            return rebuilt
        if _holds_a_declaration(arg):
            warnings.warn(
                f"{where}: an object of type {type(arg).__module__}.{type(arg).__qualname__} "
                f"cannot be rebuilt from its items, so it is deep-copied whole and a "
                f"SharedDependency, factory or extension inside it is copied instead of shared, "
                f"called or built. Give it a constructor that takes its items, or declare the "
                f"items as separate arguments.",
                RuntimeWarning,
                stacklevel=3,
            )
    try:
        cloned = copy.deepcopy(arg, memo)
    except Exception as exc:
        raise ExtensionIsolationError(
            f"Cannot isolate extension argument of type '{type(arg).__name__}' safely across "
            f"service instances. Wrap with SharedDependency(...) if sharing is intentional."
        ) from exc
    if cloned is not arg and not _is_plain_data(arg):
        _warn_that_a_copy_will_be_refused(arg, where)
    return cloned


def _snapshot_arg(arg: Any, _memo: dict[int, Any] | None = None) -> Any:
    """A private copy of a declaration argument, taken when a specification is frozen.

    The specification's attributes and its captured arguments are often the
    SAME object (`self.items = items or []`), so a change made in place through
    the attribute would otherwise rewrite what every later instance is built
    from. Nothing is called here: a factory (a callable that takes no arguments)
    runs once per bound instance, a `SharedDependency` is the one shared object,
    a nested extension freezes itself, and a type is a reference. A callable
    that takes arguments is a value like any other, and a mutable one is copied.
    An argument that cannot be copied stays the caller's object, so it fails
    loudly when an instance is bound, as it does without this. The test for a
    factory is the one binding uses, so what is copied here is what binding
    would otherwise copy.
    """
    memo: dict[int, Any] = {} if _memo is None else _memo
    if isinstance(arg, SharedDependency | Extension | type):
        return arg
    if callable(arg) and _takes_no_arguments(arg):
        return arg
    if type(arg) is tuple:
        return tuple(_snapshot_arg(item, memo) for item in arg)
    if type(arg) is list:
        if id(arg) in memo:
            return memo[id(arg)]
        snapshot_list: list[Any] = []
        memo[id(arg)] = snapshot_list
        snapshot_list.extend(_snapshot_arg(item, memo) for item in arg)
        return snapshot_list
    if type(arg) is dict:
        if id(arg) in memo:
            return memo[id(arg)]
        snapshot_dict: dict[Any, Any] = {}
        memo[id(arg)] = snapshot_dict
        for key, value in arg.items():
            snapshot_dict[key] = _snapshot_arg(value, memo)
        return snapshot_dict
    if isinstance(arg, tuple):
        rebuilt = _a_tuple_of_its_own_type(arg, [_snapshot_arg(item, memo) for item in arg])
        if rebuilt is not None:
            return rebuilt
    try:
        return copy.deepcopy(arg, memo)
    except Exception:
        return arg


class Extension:
    name: str = ""
    service: Any = None

    @property
    def _service_log(self) -> Any:
        """The loguru logger bound to the service this extension belongs to, once it is bound.

        A sink that streams one service's lines takes the records bound to that service, so the
        lines an extension writes for it carry the binding. Before the extension is bound to a
        service there is none to name, and the plain logger is returned.
        """
        name = getattr(getattr(self.service, "config", None), "name", None)
        return logger.bind(service=name) if name else logger

    _origin: Extension | None = None
    fails_closed: bool = False

    _spec_args: tuple[Any, ...] = ()
    _spec_kwargs: dict[str, Any] = {}
    _spec_frozen: bool = False

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        instance = super().__new__(cls)
        instance._spec_args = args
        instance._spec_kwargs = kwargs
        instance._spec_frozen = False
        return instance

    def __set_name__(self, owner: type, name: str) -> None:
        """Freeze a specification when a class body declares it.

        `create_instance` builds every runtime instance from the constructor
        arguments captured by `__new__`, never from the specification's
        attributes, so a write to a declared specification reaches no service
        whether it happens before or after one is built. Freezing at
        declaration makes that write raise instead of vanishing.
        """
        self.freeze()

    def freeze(self) -> None:
        """Lock specification attributes against post-declaration mutation.

        The declaration arguments are copied on the first freeze, so changing a
        declared collection in place afterwards cannot reach an instance built
        later. Idempotent: a specification shared by every instance of a service
        class is frozen again each time one is built, and a frozen one is not
        copied again.

        The guarantee covers the public attributes and the arguments. The
        underscore-prefixed attributes (``_spec_args``, ``_spec_kwargs``) are the
        machinery that does the freezing and are outside it: a write made through
        one is not guarded.
        """
        if not self._spec_frozen:
            object.__setattr__(self, "_spec_args", tuple(_snapshot_arg(a) for a in self._spec_args))
            object.__setattr__(
                self,
                "_spec_kwargs",
                {key: _snapshot_arg(value) for key, value in self._spec_kwargs.items()},
            )
        object.__setattr__(self, "_spec_frozen", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_spec_frozen", False):
            raise AttributeError(
                f"Cannot mutate attribute '{name}' on immutable extension specification "
                f"'{type(self).__name__}'. State mutations must happen on bound runtime instances."
            )
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_spec_frozen", False):
            raise AttributeError(
                f"Cannot delete attribute '{name}' on immutable extension specification "
                f"'{type(self).__name__}'."
            )
        super().__delattr__(name)

    def create_instance(self, service: Any, name: str) -> Extension:
        """Construct a runtime extension instance using cloned declaration arguments.

        Reconstructs an instance of type(self) from the declaration's arguments, each
        isolated per instance: a `SharedDependency` is passed through as its value, a
        zero-argument callable is called once, a nested `Extension` is instantiated,
        a tuple is isolated item by item, and anything else is deep-copied. An argument
        that cannot be deep-copied is refused with `ExtensionIsolationError` rather
        than shared by reference; wrap it in `SharedDependency` to share it.
        """
        owner = type(self).__name__
        args = tuple(
            _safe_clone_arg(a, None, f"{owner} argument {position}")
            for position, a in enumerate(self._spec_args, start=1)
        )
        kwargs = {
            k: _safe_clone_arg(v, None, f"{owner}({k}=...)") for k, v in self._spec_kwargs.items()
        }
        return type(self)(*args, **kwargs)

    def bind(self, service: Any, name: str) -> Extension:
        """Construct and bind a runtime instance for a service.

        The declaration attribute acts as an immutable factory specification.
        The returned runtime instance carries independent state initialized by
        its constructor, referencing this specification as its _origin.
        """
        bound = self.create_instance(service, name)
        bound.service = service
        bound.name = name
        bound._origin = self
        return bound

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        """Before the broker is connected. Read config, build per-instance state."""

    async def start(self) -> None:
        """After the broker is connected and `on_startup` has returned, before the service
        subscribes to its handlers."""

    async def stop(self) -> None:
        """On shutdown, in reverse declaration order, before the broker drains.

        Pairs with `setup()`, not `start()`: it also runs when startup stopped before `start()`,
        and when this extension's own `setup()` raised. It does not run for an extension whose
        `setup()` was never begun.
        """

    async def worker_setup(self, ctx: WorkerContext) -> None: ...

    async def worker_result(
        self, ctx: WorkerContext, result: object | None, exc: BaseException | None
    ) -> None: ...

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        """After `worker_result`, always. Not paired with `worker_setup`: it also runs for an
        extension whose setup never ran because an earlier extension refused the message, so
        read what setup stored with a default (`ctx.data.pop(key, None)`)."""

    # --- the SEND side -------------------------------------------------------
    #
    # worker_* run around a message this service CONSUMES. These two run around
    # a message it SENDS: call_rpc, call_async, call_rpc_no_wait, publish_event
    # and broadcast_message, one `ctx.kind` each.
    #
    # THE ONE THING A SEND HOOK MAY CHANGE IS `ctx.headers`. Two of the three
    # uses this exists for -- correlation injection and auth token attachment --
    # are header writes, so observation-only hooks could not do the job. The
    # send path reads `ctx.headers` back AFTER before_call and puts them on the
    # wire.
    #
    # `ctx.payload` and `ctx.subject` are read-only BY CONTRACT. A hook that
    # rewrote the payload would make the wire unpredictable for a caller that
    # just typed it, and the typed-RPC work validates payloads against the
    # handler's annotations -- a hook adding a key would surface as
    # `extra_forbidden` from a service the caller never touched. The send paths
    # pass a copy of the payload's containers, so a write to a dict, list, tuple
    # or set is discarded rather than policed. A custom object inside the
    # payload is passed through: an attribute set on it reaches the caller's
    # object, and the wire on the two publish paths. The tests say both.
    #
    # NO SEND-SIDE RejectMessage. The receive side has one because a check that
    # cannot refuse is not a check. Nothing here wants to
    # cancel a call, and a refusal channel is a one-way door -- excluded
    # deliberately, not overlooked.
    async def before_call(self, ctx: WorkerContext) -> None: ...

    async def after_call(
        self, ctx: WorkerContext, result: Any, exc: BaseException | None
    ) -> None: ...

    def health_details(self) -> dict[str, Any] | None:
        return None

    def info_details(self) -> dict[str, Any] | None:
        return None

    async def on_disconnect(self) -> None:
        """The broker connection was lost and the client is about to try to reconnect.

        Runs from the container's connection callback, in declaration order, before the
        ``ServiceConfig.on_disconnect`` slot. The client awaits that callback before it
        reconnects, and replays every subscription when it does, so this is the moment to
        unsubscribe a subject that must not be replayed: one unsubscribed here is not
        subscribed again. It is also why it must be QUICK, or hand its slow work to a task: a
        hook that waits delays the reconnect by as long as it waits. A hook that raises is
        logged and the others still run.

        A silent partition is noticed only after the client's ping timeout, so this can run
        long after the connection stopped working; it is not a fence.
        """

    async def on_listener_paused(self, subject: str, dependencies: tuple[str, ...]) -> None:
        """The service stopped consuming the listener on ``subject``: ``dependencies`` are down.

        Runs for a listener declared with ``pause_when_down``, after its durable's subscription
        was dropped. A hook that raises is logged and the others still run.
        """

    async def on_listener_resumed(self, subject: str, dependencies: tuple[str, ...]) -> None:
        """The service consumes the listener on ``subject`` again: ``dependencies`` are all up.

        Runs after the durable is bound again. A hook that raises is logged and the others
        still run.
        """

    async def on_reconnect(self) -> None:
        """The broker connection was regained after ``on_disconnect``.

        Runs from the container's connection callback, in declaration order, before the
        ``ServiceConfig.on_connect`` slot. The client is connected again and has replayed its
        subscriptions by now (ones an ``on_disconnect`` hook unsubscribed are not among them), so
        traffic flows while this runs. The client still awaits it before it considers the
        reconnect finished, so hand long work to a task.
        """


__all__ = [
    "Extension",
    "ExtensionIsolationError",
    "ExtensionSetupContext",
    "RejectMessage",
    "SharedDependency",
    "WorkerContext",
]

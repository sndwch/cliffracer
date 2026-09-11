"""The extension contract.

An extension is an object declared as a class attribute on a service, bound
per service instance, and run by the container. It never participates in the
MRO: order is declaration order, and every hook is optional.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cliffracer.core.exceptions import CliffracerError

if TYPE_CHECKING:
    from cliffracer.core.service_config import ServiceConfig


class ExtensionIsolationError(CliffracerError):
    """Raised when an extension specification argument cannot be safely isolated across service instances."""


class SharedDependency[T]:
    """Explicit opt-in wrapper allowing state to be shared across service instances."""

    def __init__(self, value: T) -> None:
        self.value = value
        self.obj = value

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
        return getattr(self.service, name)


@dataclass
class WorkerContext:
    """What one dispatch knows about itself, shared by every hook on the chain."""

    kind: str
    subject: str | None
    headers: dict[str, str]
    correlation_id: str | None
    payload: dict[str, Any]
    raw: Any = None
    data: dict[str, Any] = field(default_factory=dict)


class RejectMessage(Exception):
    """Refuse a message before its handler runs. Honoured ONLY from worker_setup.

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
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _safe_clone_arg(arg: Any) -> Any:
    """Clone mutable collections, deep-copy objects, or call factories so arguments are isolated per instance.

    Raises ExtensionIsolationError if an argument cannot be isolated, unless explicitly
    wrapped in SharedDependency.
    """
    if isinstance(arg, SharedDependency):
        return arg.value
    if callable(arg) and not isinstance(arg, type):
        try:
            return arg()
        except TypeError:
            pass
        except Exception:
            pass
    if isinstance(arg, Extension):
        return arg.create_instance(None, "")
    if isinstance(arg, tuple):
        return tuple(_safe_clone_arg(item) for item in arg)
    try:
        return copy.deepcopy(arg)
    except Exception as exc:
        raise ExtensionIsolationError(
            f"Cannot isolate extension argument of type '{type(arg).__name__}' safely across "
            f"service instances. Wrap with SharedDependency(...) if sharing is intentional."
        ) from exc


class Extension:
    name: str = ""
    service: Any = None
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

    def freeze(self) -> None:
        """Lock specification attributes against post-declaration mutation."""
        self._spec_frozen = True

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

        Reconstructs an instance of type(self) passing deep copies of mutable
        collection arguments and references to stateless or non-collection objects.
        """
        args = tuple(_safe_clone_arg(a) for a in self._spec_args)
        kwargs = {k: _safe_clone_arg(v) for k, v in self._spec_kwargs.items()}
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
        bound._spec_frozen = False
        return bound

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        """Before the broker is connected. Read config, build per-instance state."""

    async def start(self) -> None:
        """After the broker is connected and core subscriptions exist."""

    async def stop(self) -> None:
        """On shutdown, in reverse declaration order, before the broker drains."""

    async def worker_setup(self, ctx: WorkerContext) -> None: ...

    async def worker_result(
        self, ctx: WorkerContext, result: object | None, exc: BaseException | None
    ) -> None: ...

    async def worker_teardown(self, ctx: WorkerContext) -> None: ...

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
    # pass a copy, so a mutation is discarded rather than policed; there is a
    # test that says so.
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

    def entrypoint_kinds(self) -> dict[str, Callable[..., Any]]:
        """kind -> binder(service, method_name, bound_method, spec).

        Read by the container when the extension is BOUND, before ``setup``
        runs, so handler discovery can see every kind. Empty by default.
        """
        return {}


def entrypoint(kind: str, *, owner: Extension, **spec: Any) -> Callable[[Callable], Callable]:
    """Mark a method as an entrypoint of ``kind`` served by ``owner``.

    The owner is the UNBOUND class-attribute extension; the container resolves
    it to the bound copy by identity at discovery.
    """

    def mark(func: Callable) -> Callable:
        marks = func.__dict__.setdefault("_cliffracer_entrypoints", [])
        marks.append((kind, dict(spec), owner))
        return func

    return mark


__all__ = [
    "Extension",
    "ExtensionIsolationError",
    "ExtensionSetupContext",
    "RejectMessage",
    "SharedDependency",
    "WorkerContext",
    "_safe_clone_arg",
    "entrypoint",
]

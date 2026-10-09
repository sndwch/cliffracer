"""The service handler and specification registry.

A structured data repository holding discovered RPC handlers, event handlers,
timer specifications, schemas, and dependencies. It performs no
I/O, maintains no network connections, and contains no broker-specific
transport logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, fields
from typing import Any

from pydantic import BaseModel

from .dependencies import Dependency
from .typed_events import EventHandlerSpec
from .typed_rpc import HandlerSpec


@dataclass
class ServiceRegistry:
    """Repository of discovered handlers, specifications, and declarations.

    Invariants:
    - Holds in-memory specifications populated by handler discovery.
    - Never initiates network calls, event loop tasks, or NATS subscriptions.
    - All collections are mutable instances bound per service container.
    """

    rpc_handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    rpc_specs: dict[str, HandlerSpec] = field(default_factory=dict)
    event_handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    event_specs_by_subject: dict[str, EventHandlerSpec] = field(default_factory=dict)
    # Keyed by effective subject, as event_specs_by_subject is: one method may
    # declare several validated subjects, each with its own schema.
    event_schemas: dict[str, tuple[type[BaseModel], str | None]] = field(default_factory=dict)
    event_durables: dict[str, str] = field(default_factory=dict)
    event_fanout: set[str] = field(default_factory=set)
    event_pull: set[str] = field(default_factory=set)
    #: The declared dependencies each listener stops consuming on while one is down, by subject.
    event_pause_when_down: dict[str, tuple[str, ...]] = field(default_factory=dict)
    event_handler_names: dict[str, str] = field(default_factory=dict)
    broadcast_handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    timers: list[Any] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)

    def feature_counts(self) -> dict[str, int]:
        """Feature count summary for diagnostic reporting.

        Returns a dictionary mapping feature keys to integer counts of
        registered RPC handlers, event handlers, timers, and broadcast handlers.
        Each handler is counted once, under its kind: discovery registers a broadcast
        handler in `event_handlers` as well as `broadcast_handlers`, because it is
        subscribed like a listener, so `events` counts the listeners that are not
        broadcasts.
        """
        return {
            "rpc": len(self.rpc_handlers),
            "events": len(set(self.event_handlers) - set(self.broadcast_handlers)),
            "timers": len(self.timers),
            "broadcasts": len(self.broadcast_handlers),
        }

    def add_broadcast_handler(
        self, subject: str, handler: Callable[..., Any], spec: EventHandlerSpec | None
    ) -> None:
        """Record a broadcast handler added at runtime, with the spec read from its signature.

        Discovery records a spec under every subject it registers. A handler that has none
        is called with the payload as keywords, and replacing a handler that had one must not
        leave that one behind for the new handler to be validated against.
        """
        self.broadcast_handlers[subject] = handler
        self.event_handlers[subject] = handler
        self.event_fanout.add(subject)
        if spec is None:
            self.event_specs_by_subject.pop(subject, None)
        else:
            self.event_specs_by_subject[subject] = spec

    def snapshot(self) -> dict[str, Any]:
        """A copy of every collection, for `restore`.

        Read from the dataclass like `clear`, so a field added later is covered.
        """
        return {
            registered.name: type(getattr(self, registered.name))(getattr(self, registered.name))
            for registered in fields(self)
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        """Put every collection back as `snapshot` held it, in place, so references stay valid."""
        for name, saved in snapshot.items():
            current = getattr(self, name)
            if isinstance(current, list):
                current[:] = saved
            else:
                current.clear()
                current.update(saved)

    def clear(self) -> None:
        """Reset all registered handlers, schemas, and specifications.

        Test support: nothing in the framework calls it, because a service builds its registry
        once and discovery fills it once. It is kept so a test can reuse one registry between
        cases. Every field is a collection, and each is cleared: the list is read from the
        dataclass, so a field added later is reset without anyone remembering to add it, and
        ``test_the_registry_counts_each_handler_once_and_clear_resets_every_field`` fails if
        ``clear()`` leaves any field populated.
        """
        for registered in fields(self):
            getattr(self, registered.name).clear()

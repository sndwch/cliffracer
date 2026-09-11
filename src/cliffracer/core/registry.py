"""The service handler and specification registry.

A structured data repository holding discovered RPC handlers, event handlers,
timer specifications, schemas, dependencies, and entrypoints. It performs no
I/O, maintains no network connections, and contains no broker-specific
transport logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
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
    event_specs: dict[str, EventHandlerSpec] = field(default_factory=dict)
    event_specs_by_subject: dict[str, EventHandlerSpec] = field(default_factory=dict)
    event_schemas: dict[Callable[..., Any], tuple[type[BaseModel], str | None]] = field(
        default_factory=dict
    )
    event_durables: dict[str, str] = field(default_factory=dict)
    event_fanout: set[str] = field(default_factory=set)
    event_pull: set[str] = field(default_factory=set)
    event_handler_names: dict[str, str] = field(default_factory=dict)
    broadcast_handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    timers: list[Any] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    entrypoints: list[tuple[str, Any, Any, Callable[..., Any]]] = field(default_factory=list)

    def feature_counts(self) -> dict[str, int]:
        """Feature count summary for diagnostic reporting.

        Returns a dictionary mapping feature keys to integer counts of
        registered RPC handlers, event handlers, timers, and broadcast handlers.
        """
        return {
            "rpc": len(self.rpc_handlers),
            "events": len(self.event_handlers),
            "timers": len(self.timers),
            "broadcasts": len(self.broadcast_handlers),
        }

    def clear(self) -> None:
        """Reset all registered handlers, schemas, and specifications."""
        self.rpc_handlers.clear()
        self.rpc_specs.clear()
        self.event_handlers.clear()
        self.event_specs.clear()
        self.event_specs_by_subject.clear()
        self.event_schemas.clear()
        self.event_durables.clear()
        self.event_fanout.clear()
        self.event_pull.clear()
        self.event_handler_names.clear()
        self.broadcast_handlers.clear()
        self.timers.clear()
        self.dependencies.clear()
        self.entrypoints.clear()

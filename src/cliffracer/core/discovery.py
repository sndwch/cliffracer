"""The handler discovery and semantic validation engine.

Reflects on service classes and bound extensions to inspect methods decorated
with messaging markers, constructs structured handler specifications, validates
topological and configuration invariants, and populates a ServiceRegistry.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from .dependencies import Dependency
from .exceptions import ConfigurationError
from .extension import Extension
from .jetstream import StreamDeclarationError, subject_covered_by
from .registry import ServiceRegistry
from .service_config import ServiceConfig
from .typed_events import build_event_spec
from .typed_rpc import build_handler_spec


class HandlerDiscovery:
    """Stateless scanner inspecting service methods and validating invariants.

    Invariants:
    - Never mutates service instance attributes directly.
    - Scans classes via ``type(service)`` to avoid triggering property getters.
    - Raises ConfigurationError on invalid messaging or consumer topology.
    - Raises StreamDeclarationError on uncovered JetStream dead-letter subjects.
    """

    HANDLER_MARKER_PREFIX = "_cliffracer_"

    @classmethod
    def with_namespace(cls, config: ServiceConfig, subject: str) -> str:
        """Prefix a subject with the service namespace if configured."""
        ns = config.namespace
        return f"{ns}.{subject}" if ns else subject

    @classmethod
    def effective_event_subject(
        cls, config: ServiceConfig, pattern: str, cross_namespace: bool
    ) -> str:
        """Derive the wire subscription subject for an event listener pattern."""
        if cross_namespace:
            return f"*.{pattern}"
        return cls.with_namespace(config, pattern)

    @classmethod
    def discover(
        cls,
        service: Any,
        config: ServiceConfig,
        extensions: list[Extension] | None = None,
        entrypoint_kinds: dict[str, Callable[..., Any]] | None = None,
        registry: ServiceRegistry | None = None,
    ) -> ServiceRegistry:
        """Discover decorated methods on a service instance and populate a registry.

        Scans ``type(service)`` for RPC handlers, event listeners, timers,
        validated listeners, broadcast handlers, dependencies, and extension
        entrypoints. Executes all semantic invariant validations before returning.
        """
        reg = registry if registry is not None else ServiceRegistry()
        exts = extensions or []
        ep_kinds = entrypoint_kinds or {}

        # 1. Inspect decorated handlers on service class
        for name, member in inspect.getmembers(type(service)):
            if name.startswith("_"):
                continue

            markers = getattr(member, "__dict__", None)
            if not markers or not any(key.startswith(cls.HANDLER_MARKER_PREFIX) for key in markers):
                continue

            method = getattr(service, name)

            # Discover RPC handlers
            if hasattr(method, "_cliffracer_rpc"):
                reg.rpc_handlers[name] = method
                reg.rpc_specs[name] = build_handler_spec(name, method, owner=type(service))

            # Discover event listeners
            if hasattr(method, "_cliffracer_events"):
                spec = build_event_spec(name, method, owner=type(service))
                reg.event_specs[name] = spec
                cross: set[str] = getattr(method, "_cliffracer_event_cross_namespace", set())
                durables: dict[str, str] = getattr(method, "_cliffracer_event_durables", {})
                fanout: set[str] = getattr(method, "_cliffracer_event_fanout", set())
                pull: set[str] = getattr(method, "_cliffracer_event_pull", set())
                for pattern in method._cliffracer_events:
                    cls._validate_subject_type(service, pattern, method)
                    eff = cls.effective_event_subject(config, pattern, pattern in cross)
                    if eff in reg.event_handlers:
                        prev = reg.event_handler_names.get(eff, "unknown")
                        raise ConfigurationError(
                            f"Duplicate event listener declared on subject {eff!r}: "
                            f"{name!r} conflicts with {prev!r}"
                        )
                    reg.event_handlers[eff] = method
                    reg.event_specs_by_subject[eff] = spec
                    if pattern in durables:
                        reg.event_durables[eff] = durables[pattern]
                    if pattern in fanout:
                        reg.event_fanout.add(eff)
                    if pattern in pull:
                        reg.event_pull.add(eff)
                    reg.event_handler_names[eff] = name

            # Discover timers
            if hasattr(method, "_cliffracer_timers"):
                for timer_instance in method._cliffracer_timers:
                    timer_to_add = (
                        timer_instance.clone()
                        if hasattr(timer_instance, "clone")
                        else timer_instance
                    )
                    if getattr(timer_to_add, "distributed", False):
                        has_kv = any(getattr(ext, "name", None) == "kv" for ext in exts) or (
                            hasattr(service, "kv") and service.kv is not None
                        )
                        if not has_kv:
                            raise ConfigurationError(
                                f"Handler '{name}' on service '{config.name}' declares "
                                f"@cron(distributed=True), but no KvExtension is registered on the service. "
                                f"Distributed cron requires cliffracer-kv to be bound to the service."
                            )
                    reg.timers.append(timer_to_add)

            # Discover validated event listeners
            if hasattr(method, "_cliffracer_validated_events"):
                v_cross: set[str] = getattr(method, "_cliffracer_event_cross_namespace", set())
                v_durables: dict[str, str] = getattr(method, "_cliffracer_event_durables", {})
                v_fanout: set[str] = getattr(method, "_cliffracer_event_fanout", set())
                for pattern, schema, on_invalid in method._cliffracer_validated_events:
                    cls._validate_subject_type(service, pattern, method)
                    eff = cls.effective_event_subject(config, pattern, pattern in v_cross)
                    if eff in reg.event_handlers:
                        prev = reg.event_handler_names.get(eff, "unknown")
                        raise ConfigurationError(
                            f"Duplicate event listener declared on subject {eff!r}: "
                            f"{name!r} conflicts with {prev!r}"
                        )
                    reg.event_handlers[eff] = method
                    reg.event_schemas[method] = (schema, on_invalid)
                    if pattern in v_durables:
                        reg.event_durables[eff] = v_durables[pattern]
                    if pattern in v_fanout:
                        reg.event_fanout.add(eff)
                    reg.event_handler_names[eff] = name

            # Discover broadcast handlers
            if hasattr(method, "_cliffracer_broadcast"):
                spec = build_event_spec(name, method, owner=type(service))
                reg.event_specs[name] = spec
                pattern = method._cliffracer_broadcast
                cls._validate_subject_type(service, pattern, method)
                if pattern in reg.event_handlers:
                    prev = reg.event_handler_names.get(pattern, "unknown")
                    raise ConfigurationError(
                        f"Duplicate event listener declared on subject {pattern!r}: "
                        f"{name!r} conflicts with {prev!r}"
                    )
                reg.event_fanout.add(pattern)
                reg.event_handler_names[pattern] = name
                reg.broadcast_handlers[pattern] = method
                reg.event_handlers[pattern] = method
                reg.event_specs_by_subject[pattern] = spec

            # Discover and bind extension entrypoints
            for kind, spec, owner in markers.get("_cliffracer_entrypoints", []):
                reg.entrypoints.append((kind, spec, owner, method))
                binder = ep_kinds.get(kind)
                if binder is None:
                    raise TypeError(
                        f"{type(service).__name__}.{name}: no extension registers "
                        f"entrypoint kind {kind!r}"
                    )
                bound_ext = next((e for e in exts if getattr(e, "_origin", None) is owner), None)
                if bound_ext is None:
                    raise TypeError(
                        f"{type(service).__name__}.{name}: entrypoint kind {kind!r} names an "
                        f"extension that is not declared on this service"
                    )
                binder(service, name, method, spec)

        # 2. Discover @dependency markers
        cls.discover_dependencies(service, reg)

        # 3. Validate semantic invariants
        cls.validate_pull_is_usable(reg, config)
        cls.validate_fanout_and_durable_are_exclusive(reg, config)
        cls.validate_fanout_declared(reg, config)
        cls.validate_unique_durables(reg)

        return reg

    @classmethod
    def _validate_subject_type(cls, service: Any, subject: Any, method: Callable[..., Any]) -> None:
        """Refuse event subjects that are not strings."""
        if not isinstance(subject, str):
            raise TypeError(
                f"{type(service).__name__}: event subject must be a str, got "
                f"{type(subject).__name__} ({subject!r}) from handler "
                f"{getattr(method, '__name__', method)!r}"
            )

    @classmethod
    def discover_dependencies(cls, service: Any, registry: ServiceRegistry) -> None:
        """Scan service class MRO for @dependency probe markers."""
        found: dict[str, list[tuple[str, type, dict[str, Any]]]] = {}
        for attr, member in inspect.getmembers(type(service)):
            spec = getattr(member, "_cliffracer_dependency", None)
            if spec is None:
                continue
            owner = next(k for k in type(service).__mro__ if attr in vars(k))
            found.setdefault(spec["name"], []).append((attr, owner, spec))

        seen: dict[str, Dependency] = {}
        for name, declarations in found.items():
            attr, _, spec = cls._resolve_one_dependency(service, name, declarations)
            seen[name] = Dependency(
                name=name,
                probe=getattr(service, attr),
                timeout=spec["timeout"],
                detail=dict(spec["detail"]),
            )
        registry.dependencies = [seen[key] for key in sorted(seen)]

    @classmethod
    def _resolve_one_dependency(
        cls,
        service: Any,
        name: str,
        declarations: list[tuple[str, type, dict[str, Any]]],
    ) -> tuple[str, type, dict[str, Any]]:
        """Resolve a single dependency declaration or reject duplicate conflicting claims."""
        if len(declarations) == 1:
            return declarations[0]

        ordered = sorted(declarations, key=lambda d: type(service).__mro__.index(d[1]))
        winner = ordered[0]
        if all(
            other[1] is not winner[1] and issubclass(winner[1], other[1]) for other in ordered[1:]
        ):
            return winner

        where = ", ".join(f"{owner.__name__}.{attr}" for attr, owner, _ in ordered)
        raise ConfigurationError(
            f"dependency name {name!r} is declared more than once, by {where}. "
            f"A name is one key in the /health payload, so only one probe can "
            f"report under it -- the others would be dropped silently. Give "
            f"each dependency its own name, or, to replace a base class's "
            f"probe, declare the same name on a subclass."
        )

    @classmethod
    def validate_fanout_declared(cls, registry: ServiceRegistry, config: ServiceConfig) -> None:
        """Refuse a listener that is neither queue-grouped nor deliberately fanned out."""
        declared = set(registry.event_fanout)
        if config.jetstream_enabled:
            declared |= set(registry.event_durables)
        offenders = sorted(
            subject for subject in registry.event_handlers if subject not in declared
        )
        if not offenders:
            return

        inert = {
            subject: registry.event_durables[subject]
            for subject in offenders
            if subject in registry.event_durables
        }

        listed = "\n".join(
            f"  {subject!r}  (handler {registry.event_handler_names.get(subject, '?')})"
            + (
                f" declares durable {inert[subject]!r}, which is inert while "
                f"jetstream_enabled is False"
                if subject in inert
                else " declares neither a durable nor fanout"
            )
            for subject in offenders
        )
        if len(inert) == len(offenders):
            headline = (
                f"{len(offenders)} listener(s) on {config.name} declare a "
                f"durable that jetstream_enabled=False makes inert"
            )
            remedy = (
                "Choose one:\n"
                "  jetstream_enabled=True  the durable works, one replica per message\n"
                "  fanout=True             every replica handles every message, on purpose\n"
            )
        else:
            headline = (
                f"{len(offenders)} listener(s) on {config.name} declare neither "
                f"a durable nor fanout"
            )
            remedy = (
                "Choose one:\n"
                '  durable="<name>"  one replica per message (needs jetstream_enabled)\n'
                "  fanout=True       every replica handles every message, on purpose\n"
            )
        raise ConfigurationError(
            f"{headline}:\n{listed}\n\n"
            f"A listener with no queue group means every replica handles every message.\n"
            + remedy
            + "fanout=True is required so that broadcast delivery is a deliberate decision."
        )

    @classmethod
    def validate_pull_is_usable(cls, registry: ServiceRegistry, config: ServiceConfig) -> None:
        """Refuse pull consumers lacking durables, claiming fanout, or without JetStream."""
        for subject in sorted(registry.event_pull):
            if subject in registry.event_fanout:
                raise ConfigurationError(
                    f"{subject!r} declares both pull=True and fanout=True. A pull "
                    f"consumer delivers each message to ONE replica; fanout "
                    f"delivers it to all of them. Drop whichever is not meant."
                )
            if subject not in registry.event_durables:
                raise ConfigurationError(
                    f"{subject!r} declares pull=True with no durable. A pull "
                    f"consumer IS a durable consumer -- there is nothing to pull "
                    f'from without one. Add durable="<name>".'
                )
            if not config.jetstream_enabled:
                raise ConfigurationError(
                    f"{subject!r} declares pull=True but this service has "
                    f"jetstream_enabled=False. Core NATS has no pull consumers, "
                    f"and quietly falling back to a core subscription would make "
                    f"every replica handle every message. Set jetstream_enabled=True "
                    f"or drop pull=True."
                )

    @classmethod
    def validate_fanout_and_durable_are_exclusive(
        cls, registry: ServiceRegistry, config: ServiceConfig
    ) -> None:
        """Refuse subjects declaring both single-replica durable and all-replica fanout."""
        if not config.jetstream_enabled:
            return
        both = sorted(set(registry.event_durables) & registry.event_fanout)
        if both:
            listed = ", ".join(repr(s) for s in both)
            raise ConfigurationError(
                f"{listed} declare(s) BOTH a durable and fanout=True. They mean "
                f"opposite things: a durable delivers each message to one "
                f"replica, fanout delivers it to all of them. Drop whichever is "
                f"not what you meant."
            )

    @classmethod
    def validate_unique_durables(cls, registry: ServiceRegistry) -> None:
        """Refuse multiple event subjects sharing identical durable consumer names."""
        by_durable: dict[str, list[str]] = {}
        for subject, durable in registry.event_durables.items():
            by_durable.setdefault(durable, []).append(subject)

        for durable, subjects in sorted(by_durable.items()):
            if len(subjects) > 1:
                listed = ", ".join(repr(s) for s in sorted(subjects))
                raise ConfigurationError(
                    f"durable {durable!r} is claimed by {len(subjects)} event "
                    f"subjects: {listed}. A durable consumer has exactly one "
                    f"filter subject, so only one of these would ever be "
                    f"delivered -- silently, with an empty DLQ and no error. "
                    f"Give each subject its own durable name."
                )

    @classmethod
    def validate_dlq_coverage(cls, config: ServiceConfig) -> None:
        """Refuse active JetStream configuration lacking stream coverage for DLQ subject."""
        if not config.jetstream_enabled:
            return
        dlq_subject = config.dlq_subject.format(
            service=config.name,
            namespace=config.namespace or "",
        )
        if subject_covered_by(config.jetstream_streams, dlq_subject):
            return

        claims = [s for spec in config.jetstream_streams for s in spec.subjects]
        raise StreamDeclarationError(
            f"jetstream_enabled is on, but no declared stream covers the dead-letter "
            f"subject {dlq_subject!r}. Declared claims: {claims}."
        )

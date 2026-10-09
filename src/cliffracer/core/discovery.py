"""The handler discovery and semantic validation engine.

Reflects on service classes and bound extensions to inspect methods decorated
with messaging markers, constructs structured handler specifications, validates
topological and configuration invariants, and populates a ServiceRegistry.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, ClassVar

from loguru import logger

from .dependencies import Dependency
from .exceptions import ConfigurationError
from .extension import Extension
from .jetstream import StreamDeclarationError, subject_covered_by
from .registry import ServiceRegistry
from .service_config import ServiceConfig
from .subjects import subjects_overlap
from .typed_events import build_event_spec, build_validated_event_spec
from .typed_rpc import build_handler_spec


class HandlerDiscovery:
    """Stateless scanner inspecting service methods and validating invariants.

    Invariants:
    - Never mutates service instance attributes directly.
    - Scans classes via ``type(service)`` to avoid triggering property getters.
    - Raises ConfigurationError on invalid messaging or consumer topology.
    - Raises TypeError on a hand-written event marker whose subject is not a str. The
      decorators refuse a non-str subject themselves, with a ConfigurationError, so this
      is reachable only past them.
    - Raises StreamDeclarationError on uncovered JetStream dead-letter subjects, from
      ``validate_dlq_coverage``, which the container runs at startup and ``discover`` does not.
    """

    HANDLER_MARKER_PREFIX = "_cliffracer_"

    @classmethod
    def scoped_subject(
        cls, subject: str, *, namespace: str | None, subject_prefix: str | None
    ) -> str:
        """The one place the prefix/namespace order is decided.

        ``<prefix>.<namespace>.<subject>``: the environment prefix goes outside
        the namespace. Takes the two values rather than a ``ServiceConfig``,
        because the callers that had this wrong are the ones that hold no
        config -- generated clients and the describe CLI -- and reach for the
        environment variable the field defaults from instead.

        Every path composes the order here and nowhere else: the service through
        ``with_namespace``, a generated client, and the describe CLI. A caller
        that composes it itself and gets the order the other way round asks on a
        subject nothing is subscribed to, and the ``RpcNoRespondersError`` it
        gets names a subject that reads as entirely correct.
        """
        scoped = f"{namespace}.{subject}" if namespace else subject
        return f"{subject_prefix}.{scoped}" if subject_prefix else scoped

    @classmethod
    def with_namespace(cls, config: ServiceConfig, subject: str) -> str:
        """Prefix a subject with the service namespace and environment prefix.

        Delegates, so the config-holding path and the environment-reading paths
        cannot disagree about the order.
        """
        return cls.scoped_subject(
            subject, namespace=config.namespace, subject_prefix=config.subject_prefix
        )

    @classmethod
    def dlq_subject(cls, config: ServiceConfig) -> str:
        """The dead-letter subject this service publishes to.

        A wire subject like any other: the template is formatted, then the
        environment prefix goes on. Two copies of this existed -- one here and
        one on the dead-letter publisher -- and only one of them was prefixed,
        which made every JetStream service refuse to start under a prefix
        because the coverage check compared an unprefixed subject against a
        prefixed claim.
        """
        subject = config.dlq_subject.format(
            service=config.name,
            namespace=config.namespace or "",
        )
        prefix = config.subject_prefix
        return f"{prefix}.{subject}" if prefix else subject

    @classmethod
    def outbound_subject(
        cls,
        config: ServiceConfig,
        service: str,
        verb: str,
        method: str,
        *,
        namespace: str | None = None,
    ) -> str:
        """The wire subject for a call to another service.

        The target's namespace, because the callee subscribes under its own;
        this service's environment prefix, because both are in it. Held here
        rather than repeated at each call site: three copies of this shape is
        how one of them came to miss the prefix.
        """
        target_ns = namespace if namespace is not None else config.namespace
        subject = f"{service}.{verb}.{method}"
        if target_ns:
            subject = f"{target_ns}.{subject}"
        prefix = config.subject_prefix
        return f"{prefix}.{subject}" if prefix else subject

    @classmethod
    def call_subject(
        cls,
        service: str,
        verb: str,
        method: str,
        *,
        namespace: str | None,
        subject_prefix: str | None,
    ) -> str:
        """The wire subject for a call to `service`'s `method` from a caller that holds no
        `ServiceConfig`, given the target's namespace and the environment prefix outright."""
        return cls.scoped_subject(
            f"{service}.{verb}.{method}", namespace=namespace, subject_prefix=subject_prefix
        )

    @classmethod
    def effective_event_subject(
        cls, config: ServiceConfig, pattern: str, cross_namespace: bool
    ) -> str:
        """Derive the wire subscription subject for an event listener pattern."""
        if cross_namespace:
            # The wildcard spans the namespace only. The environment prefix stays
            # outside it, so a listener that reads every namespace still reads
            # only its own environment.
            prefix = config.subject_prefix
            return f"{prefix}.*.{pattern}" if prefix else f"*.{pattern}"
        return cls.with_namespace(config, pattern)

    @classmethod
    def _refuse_cross_namespace_without_a_namespace(
        cls, owner: type, config: ServiceConfig, name: str, pattern: str
    ) -> None:
        """`cross_namespace=True` spans namespaces, so a service with none has nothing to span.

        The subscription is `*.<pattern>`, and `*` is exactly one token: it matches a publisher in
        any namespace and never a publisher with no namespace, which publishes plain `<pattern>`.
        On a service with no namespace that is every publisher it could be reading, so the
        service would start, hold a subscription, and never receive an event.
        """
        if config.namespace:
            return
        raise ConfigurationError(
            f"{owner.__name__}.{name}: listener on {pattern!r} sets cross_namespace=True, "
            f"but {config.name!r} has no namespace to span. It would subscribe to "
            f"'*.{pattern}', which matches a publisher in any namespace and never a publisher "
            f"with none, so it would receive nothing. Set a namespace on the ServiceConfig, or "
            f"drop cross_namespace=True."
        )

    @classmethod
    def discover(
        cls,
        service: Any,
        config: ServiceConfig,
        extensions: list[Extension] | None = None,
        registry: ServiceRegistry | None = None,
    ) -> ServiceRegistry:
        """Discover decorated methods on a service instance and populate a registry.

        Scans ``type(service)`` for RPC handlers, event listeners, timers,
        validated listeners, broadcast handlers and dependencies. Runs the four listener validations before returning: pull
        consumers, fanout against durable, fanout declared, and unique durables.
        It does not run ``validate_dlq_coverage``: the container checks that the
        dead-letter subject is covered by a declared stream at startup, so a caller
        that uses ``discover`` alone has not had that check.

        Call it once per registry. A second call into the same registry refuses the first
        duplicate listener it meets, and a service with only timers gets a second copy of each
        one: the container keeps a flag so that its own registry is scanned once. If it raises,
        the registry holds what was scanned before the failure; the container restores its own
        from a snapshot, and a caller that passes a registry of its own should discard it.
        """
        reg = registry if registry is not None else ServiceRegistry()
        exts = extensions or []

        # 1. Inspect decorated handlers on service class
        for name, member in inspect.getmembers(type(service)):
            if name.startswith("_"):
                cls._refuse_a_private_handler(type(service), name, member)
                continue

            if not cls._carries_a_handler_marker(member):
                cls._warn_about_an_undecorated_override(service, name)

            markers = getattr(member, "__dict__", None)
            if not markers or not any(key.startswith(cls.HANDLER_MARKER_PREFIX) for key in markers):
                continue

            cls._refuse_a_handler_that_replaces_a_framework_method(type(service), name, member)
            method = getattr(service, name)

            # Discover RPC handlers
            if hasattr(method, "_cliffracer_rpc"):
                reg.rpc_handlers[name] = method
                reg.rpc_specs[name] = build_handler_spec(name, method, owner=type(service))

            # Discover event listeners
            if hasattr(method, "_cliffracer_events"):
                spec = build_event_spec(name, method, owner=type(service))
                cross: set[str] = getattr(method, "_cliffracer_event_cross_namespace", set())
                durables: dict[str, str] = getattr(method, "_cliffracer_event_durables", {})
                fanout: set[str] = getattr(method, "_cliffracer_event_fanout", set())
                pull: set[str] = getattr(method, "_cliffracer_event_pull", set())
                pauses: dict[str, tuple[str, ...]] = getattr(
                    method, "_cliffracer_event_pause_when_down", {}
                )
                for pattern in method._cliffracer_events:
                    cls._validate_subject_type(service, pattern, method)
                    if pattern in cross:
                        cls._refuse_cross_namespace_without_a_namespace(
                            type(service), config, name, pattern
                        )
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
                    if pattern in pauses:
                        reg.event_pause_when_down[eff] = pauses[pattern]
                    reg.event_handler_names[eff] = name

            # Discover timers
            if hasattr(method, "_cliffracer_timers"):
                for timer_instance in method._cliffracer_timers:
                    timer_to_add = (
                        timer_instance.clone()
                        if hasattr(timer_instance, "clone")
                        else timer_instance
                    )
                    check = getattr(timer_to_add, "check_declared_dependencies", None)
                    if check is not None:
                        check(service, name, exts)
                    reg.timers.append(timer_to_add)

            # Discover validated event listeners
            if hasattr(method, "_cliffracer_validated_events"):
                v_cross: set[str] = getattr(method, "_cliffracer_event_cross_namespace", set())
                v_durables: dict[str, str] = getattr(method, "_cliffracer_event_durables", {})
                v_fanout: set[str] = getattr(method, "_cliffracer_event_fanout", set())
                v_pauses: dict[str, tuple[str, ...]] = getattr(
                    method, "_cliffracer_event_pause_when_down", {}
                )
                for pattern, schema, on_invalid in method._cliffracer_validated_events:
                    spec = build_validated_event_spec(
                        name,
                        method,
                        owner=type(service),
                        schema=schema,
                    )
                    cls._validate_subject_type(service, pattern, method)
                    if pattern in v_cross:
                        cls._refuse_cross_namespace_without_a_namespace(
                            type(service), config, name, pattern
                        )
                    eff = cls.effective_event_subject(config, pattern, pattern in v_cross)
                    if eff in reg.event_handlers:
                        prev = reg.event_handler_names.get(eff, "unknown")
                        raise ConfigurationError(
                            f"Duplicate event listener declared on subject {eff!r}: "
                            f"{name!r} conflicts with {prev!r}"
                        )
                    reg.event_handlers[eff] = method
                    reg.event_specs_by_subject[eff] = spec
                    reg.event_schemas[eff] = (schema, on_invalid)
                    if pattern in v_durables:
                        reg.event_durables[eff] = v_durables[pattern]
                    if pattern in v_fanout:
                        reg.event_fanout.add(eff)
                    if pattern in v_pauses:
                        reg.event_pause_when_down[eff] = v_pauses[pattern]
                    reg.event_handler_names[eff] = name

            # Discover broadcast handlers
            if hasattr(method, "_cliffracer_broadcast"):
                spec = build_event_spec(name, method, owner=type(service))
                pattern = method._cliffracer_broadcast
                cls._validate_subject_type(service, pattern, method)
                # The subject broadcast_message publishes to, as a @listener
                # without cross_namespace subscribes to it.
                eff = cls.effective_event_subject(config, pattern, False)
                if eff in reg.event_handlers:
                    prev = reg.event_handler_names.get(eff, "unknown")
                    raise ConfigurationError(
                        f"Duplicate event listener declared on subject {eff!r}: "
                        f"{name!r} conflicts with {prev!r}"
                    )
                reg.event_fanout.add(eff)
                reg.event_handler_names[eff] = name
                reg.broadcast_handlers[eff] = method
                reg.event_handlers[eff] = method
                reg.event_specs_by_subject[eff] = spec

        # 2. Discover @dependency markers
        cls.discover_dependencies(service, reg)

        # 3. Validate semantic invariants
        cls.validate_pull_is_usable(reg, config)
        cls.validate_fanout_and_durable_are_exclusive(reg, config)
        cls.validate_fanout_declared(reg, config)
        cls.validate_unique_durables(reg)
        cls.validate_pause_when_down_is_durable(reg, config)
        cls.validate_pause_when_down_names(reg)

        return reg

    @classmethod
    def _refuse_a_handler_that_replaces_a_framework_method(
        cls, owner: type, name: str, member: Any
    ) -> None:
        """Refuse a decorated method named for one `CliffracerService` already defines.

        A decorator only marks the method, so the decorated method is the
        service's `name` for everything that calls it, the framework included:
        a `@timer` called `health_check` replaces the method `/health` answers
        from, and the probe then fails on what the timer returns.
        """
        from .service import CliffracerService

        base = getattr(CliffracerService, name, None)
        shadowed = name in cls._attributes_a_service_sets_in_its_constructor()
        if (base is not None and member is not base) or shadowed:
            raise ConfigurationError(
                f"{owner.__name__}.{name} is a decorated handler, but "
                f"CliffracerService defines a method named {name!r} that the framework "
                f"calls. The handler would replace it for the whole service. "
                f"Give the handler another name."
            )

    @staticmethod
    @functools.cache
    def _attributes_a_service_sets_in_its_constructor() -> frozenset[str]:
        """The public attribute names every `CliffracerService` instance carries.

        `config`, `logger` and `health_listener` are set in the constructor, so they are not
        class attributes and `getattr(CliffracerService, name)` does not find them; an instance
        attribute shadows a method of the same name, so a handler given one of those names is
        advertised by `describe` and never registered. Read off a constructed service, as
        `reserved_rpc_method_names` reads a client, so an attribute added to the constructor is
        reserved with it.
        """
        from .service import CliffracerService
        from .service_config import ServiceConfig

        probe = CliffracerService(
            ServiceConfig(name="reserved", subject_prefix=None, health_port=0)
        )
        return frozenset(name for name in vars(probe) if not name.startswith("_"))

    @classmethod
    def _validate_subject_type(cls, service: Any, subject: Any, method: Callable[..., Any]) -> None:
        """Refuse an event subject that is not a string, in a marker the decorators did not write.

        ``listener``, ``validated_listener`` and ``broadcast`` refuse a non-str subject
        when they decorate, with a ConfigurationError, before any marker exists. This
        guards a marker set by hand, and raises TypeError.
        """
        if not isinstance(subject, str):
            raise TypeError(
                f"{type(service).__name__}: event subject must be a str, got "
                f"{type(subject).__name__} ({subject!r}) from handler "
                f"{getattr(method, '__name__', method)!r}"
            )

    #: The markers `discover` reads as a handler, and the decorator that sets each. A name that
    #: starts with an underscore is skipped, so a handler marked on one would register nothing.
    #: `@dependency` is not here: its probes are canonically `_check_db`, and are read separately.
    _HANDLER_DECORATORS: ClassVar[dict[str, str]] = {
        "_cliffracer_rpc": "@rpc or @async_rpc",
        "_cliffracer_events": "@listener",
        "_cliffracer_validated_events": "@validated_listener",
        "_cliffracer_broadcast": "@broadcast",
        "_cliffracer_timers": "@timer",
    }

    @classmethod
    def _refuse_a_private_handler(cls, owner: type, name: str, member: Any) -> None:
        """Refuse a handler marker on an underscore-prefixed member, which discovery would skip."""
        markers = getattr(member, "__dict__", None) or {}
        carried = [
            decorator for key, decorator in cls._HANDLER_DECORATORS.items() if key in markers
        ]
        if carried:
            raise ConfigurationError(
                f"{owner.__name__}.{name} is decorated with {' and '.join(carried)}, but "
                f"its name starts with an underscore and discovery skips those: it would register "
                f"nothing, subscribe to nothing, and say nothing. Rename it without the leading "
                f"underscore, or remove the decorator."
            )

    @classmethod
    def _carries_a_handler_marker(cls, member: Any) -> bool:
        markers = getattr(member, "__dict__", None) or {}
        return any(key in markers for key in cls._HANDLER_DECORATORS)

    @classmethod
    def _warn_about_an_undecorated_override(cls, service: Any, name: str) -> None:
        """Say so when a subclass's override of a decorated handler is not itself decorated.

        Discovery reads the markers off the member the class resolves, so an override with no
        decorator registers nothing: the base's handler is silently gone, and the service starts
        and says nothing. That can be what the override is for (switching an inherited handler
        off), so it is a warning, not a refusal. There is no marker for "deliberately off": to keep
        the handler, put the decorator on the override.
        """
        klass = type(service)
        mro = klass.__mro__
        owner = next((k for k in mro if name in vars(k)), None)
        if owner is None:
            return
        for base in mro[mro.index(owner) + 1 :]:
            inherited = vars(base).get(name)
            decorators = [
                decorator
                for key, decorator in cls._HANDLER_DECORATORS.items()
                if key in (getattr(inherited, "__dict__", None) or {})
            ]
            if decorators:
                name_of_service = getattr(getattr(service, "config", None), "name", None)
                log = logger.bind(service=name_of_service) if name_of_service else logger
                log.warning(
                    f"{owner.__name__}.{name} overrides {base.__name__}.{name}, which is decorated "
                    f"with {', '.join(decorators)}, but the override is not: it registers "
                    f"nothing, so this {klass.__name__} does not serve that handler. If that is "
                    f"deliberate, nothing more is needed; to keep the handler, put the decorator "
                    f"on {owner.__name__}.{name}."
                )
                return

    @classmethod
    def discover_dependencies(cls, service: Any, registry: ServiceRegistry) -> None:
        """Scan service class MRO for @dependency probe markers.

        Adds to the registry's dependencies; it does not replace them. A probe the service
        registered at runtime (`add_dependency`) is already there, and discovery runs again at
        `start()`. A name that is both declared and already registered keeps the registered one,
        the one `add_dependency` put there to replace the declared probe.
        """
        found: dict[str, list[tuple[str, type, dict[str, Any]]]] = {}
        for attr, member in inspect.getmembers(type(service)):
            spec = getattr(member, "_cliffracer_dependency", None)
            if spec is None:
                continue
            owner = next(k for k in type(service).__mro__ if attr in vars(k))
            found.setdefault(spec["name"], []).append((attr, owner, spec))

        seen: dict[str, Dependency] = {dep.name: dep for dep in registry.dependencies}
        for name, declarations in found.items():
            attr, _, spec = cls._resolve_one_dependency(service, name, declarations)
            seen.setdefault(
                name,
                Dependency(
                    name=name,
                    probe=getattr(service, attr),
                    timeout=spec["timeout"],
                    detail=dict(spec["detail"]),
                ),
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
    def validate_pause_when_down_is_durable(
        cls, registry: ServiceRegistry, config: ServiceConfig
    ) -> None:
        """Refuse `pause_when_down` on a listener that has no durable to stop consuming.

        Pausing a durable leaves its messages in the stream until the replica consumes again. A
        core subscription has no stream behind it, so pausing one would lose what is published
        meanwhile; a fanout listener broadcasts, and is the same. A durable on a service without
        JetStream is inert, so there is nothing to pause either.
        """
        for subject in sorted(registry.event_pause_when_down):
            names = registry.event_pause_when_down[subject]
            if subject not in registry.event_durables or subject in registry.event_fanout:
                raise ConfigurationError(
                    f"{subject!r} declares pause_when_down={names!r} but is not a durable "
                    f"listener. Pausing keeps messages in a JetStream stream until consumption "
                    f'resumes; a core or fanout subscription would lose them. Add durable="<name>" '
                    f"(without fanout), or drop pause_when_down."
                )
            if not config.jetstream_enabled:
                raise ConfigurationError(
                    f"{subject!r} declares pause_when_down={names!r} but this service has "
                    f"jetstream_enabled=False, so its durable is inert and there is no consumer "
                    f"to pause. Set jetstream_enabled=True or drop pause_when_down."
                )

    @classmethod
    def validate_pause_when_down_names(cls, registry: ServiceRegistry) -> None:
        """Refuse a `pause_when_down` name that no declared dependency carries.

        A name nothing probes would never be seen down, so the listener would never pause: the
        declaration would read as a safeguard that is not there. A dependency added with
        `add_dependency` counts once it is added, before the service starts.
        """
        declared = {dep.name for dep in registry.dependencies}
        for subject in sorted(registry.event_pause_when_down):
            unknown = [
                name for name in registry.event_pause_when_down[subject] if name not in declared
            ]
            if unknown:
                raise ConfigurationError(
                    f"{subject!r} declares pause_when_down on {unknown}, which no declared "
                    f"dependency is called; declared: {sorted(declared) or 'none'}. Declare it "
                    f'with @dependency("<name>") or add_dependency before the service starts.'
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
    def validate_durable_coverage(cls, registry: ServiceRegistry, config: ServiceConfig) -> None:
        """Refuse a durable listener whose subject no declared stream could carry.

        Coverage is read from the declared streams, not from what happens to be on
        the broker, so the answer is the same in a fresh environment as in a shared
        one. A durable consumer is created on a stream; one whose subject overlaps no
        declared stream fails at the last step of startup with the server's
        "not found", naming neither the subject nor the handler, after `on_startup`
        and the timers have already run.
        """
        if not config.jetstream_enabled:
            return
        declared = config.effective_jetstream_streams
        uncovered = [
            subject
            for subject in sorted(registry.event_durables)
            if not any(
                subjects_overlap(pattern, subject) for spec in declared for pattern in spec.subjects
            )
        ]
        if not uncovered:
            return
        claims = [s for spec in declared for s in spec.subjects]
        listed = "\n".join(
            f"  - {subject!r}: durable {registry.event_durables[subject]!r}, handler "
            f"{registry.event_handler_names.get(subject, 'unknown')!r}"
            for subject in uncovered
        )
        raise StreamDeclarationError(
            f"jetstream_enabled is on, but no declared stream carries the subject of "
            f"{len(uncovered)} durable listener(s) on {config.name!r}:\n{listed}\n"
            f"Declared claims: {claims}. Declare a stream whose subjects include each, "
            f"in jetstream_streams."
        )

    @classmethod
    def validate_workqueue_consumers(cls, registry: ServiceRegistry, config: ServiceConfig) -> None:
        """Refuse two durable listeners whose subjects overlap on a workqueue stream.

        A workqueue stream delivers each message to one consumer, so the server refuses a durable
        whose filter overlaps another's on the same stream: `err_code=10100`, "filtered consumer
        not unique on workqueue stream", at the last step of startup and naming neither the
        stream, the subjects nor the handlers. The service's own durables are known from
        discovery, so the pair is refused before it connects. Consumers other services create on
        the same stream are not visible here and are still the server's to refuse.
        """
        if not config.jetstream_enabled:
            return
        for spec in config.effective_jetstream_streams:
            if spec.retention != "workqueue":
                continue
            hosted = sorted(
                subject
                for subject in registry.event_durables
                if any(subjects_overlap(pattern, subject) for pattern in spec.subjects)
            )
            clashes = [
                (first, second)
                for i, first in enumerate(hosted)
                for second in hosted[i + 1 :]
                if subjects_overlap(first, second)
            ]
            if not clashes:
                continue
            listed = "\n".join(
                f"  - {first!r} (durable {registry.event_durables[first]!r}, handler "
                f"{registry.event_handler_names.get(first, 'unknown')!r}) and {second!r} "
                f"(durable {registry.event_durables[second]!r}, handler "
                f"{registry.event_handler_names.get(second, 'unknown')!r})"
                for first, second in clashes
            )
            raise StreamDeclarationError(
                f"stream {spec.name!r} has retention='workqueue', which delivers each message to "
                f"one consumer, but {len(clashes)} pair(s) of durable listeners on "
                f"{config.name!r} overlap on it:\n{listed}\n"
                f"The server refuses the second durable of each pair. Narrow the subjects so they "
                f"do not overlap, or declare the stream with retention='limits' or 'interest'."
            )

    @classmethod
    def validate_serialization_available(cls, config: ServiceConfig) -> None:
        """Refuse `serialization_format="msgpack"` when the `msgpack` package is not installed.

        The package is an optional extra. Without it the service would construct, connect and serve,
        and the first publish, call or reply that serialises would raise `ImportError` inside a
        handler. The condition is known before anything is connected.
        """
        from . import validation

        if config.serialization_format == "msgpack" and validation.msgpack is None:
            raise ConfigurationError(
                f"{config.name!r} sets serialization_format='msgpack', but the 'msgpack' package "
                f"is not installed. Install it with: pip install 'cliffracer[msgpack]', or use "
                f"serialization_format='json'."
            )

    @classmethod
    def validate_dlq_coverage(cls, config: ServiceConfig) -> None:
        """Refuse active JetStream configuration lacking stream coverage for DLQ subject."""
        if not config.jetstream_enabled:
            return
        dlq_subject = cls.dlq_subject(config)
        if subject_covered_by(config.effective_jetstream_streams, dlq_subject):
            return

        claims = [s for spec in config.effective_jetstream_streams for s in spec.subjects]
        raise StreamDeclarationError(
            f"jetstream_enabled is on, but no declared stream covers the dead-letter "
            f"subject {dlq_subject!r}. Declared claims: {claims}."
        )

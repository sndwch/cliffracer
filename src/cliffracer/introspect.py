"""Describe service classes, RPC signatures, and interface hashes for client generation."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Annotated, Any, get_args, get_origin

from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.outputs import OutputDescription, describe_outputs
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.typed_events import build_event_spec, build_validated_event_spec
from cliffracer.core.typed_rpc import (
    ParamSpec,
    build_handler_spec,
    collect_model_schemas,
    type_ref,
)


def canonical(obj: Any) -> str:
    """The one serialisation the hashes are taken over.

    Sorted keys and no whitespace, so two processes that agree about the
    content agree about the bytes.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj).encode()).hexdigest()


@dataclass(frozen=True)
class Param:
    name: str
    type: dict[str, Any]
    has_default: bool = False
    default: Any = None
    #: Whether the model or models in `default` can be rebuilt from it: the service validated
    #: the default's own dump, leniently, and got the same value, which dumps to the same JSON.
    #: Present only on a parameter with a default whose type holds a model; None otherwise, and
    #: for a description that predates it, which a client generator reads as "do not rebuild".
    rebuildable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        # `default` is ABSENT rather than null when there is none: a parameter
        # defaulting to None and one with no default are different things to a
        # generated client, and null cannot tell them apart.
        d: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.has_default:
            d["default"] = self.default
        if self.rebuildable is not None:
            d["rebuildable"] = self.rebuildable
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Param:
        rebuildable = d.get("rebuildable")
        return cls(
            name=d["name"],
            type=d["type"],
            has_default="default" in d,
            default=d.get("default"),
            rebuildable=rebuildable if isinstance(rebuildable, bool) else None,
        )


def _holds_a_model(ref: dict[str, Any]) -> bool:
    kind = ref.get("kind")
    if kind == "model":
        return True
    inner = ref.get("item") if kind == "list" else ref.get("value") if kind == "dict" else None
    inner = ref.get("inner") if kind == "optional" else inner
    return isinstance(inner, dict) and _holds_a_model(inner)


def _unannotated(tp: Any) -> Any:
    while get_origin(tp) is Annotated:
        tp = get_args(tp)[0]
    return tp


def _rebuilt_as_a_client_does(tp: Any, ref: dict[str, Any], dump: Any) -> Any:
    """`dump` with each model in it built the way a generated client builds it.

    The client calls `Model.model_validate(dump, strict=False)` for each model it finds in the
    default's type and leaves everything else as the dump has it; it does not apply what the
    parameter's annotation adds around a model (`Annotated` validators and constraints).
    """
    tp = _unannotated(tp)
    kind = ref["kind"]
    if kind == "model" and isinstance(dump, dict):
        return tp.model_validate(dump, strict=False)
    if kind == "optional" and dump is not None:
        inner = next(a for a in get_args(tp) if _unannotated(a) is not type(None))
        return _rebuilt_as_a_client_does(inner, ref["inner"], dump)
    if kind == "list" and isinstance(dump, list):
        return [_rebuilt_as_a_client_does(get_args(tp)[0], ref["item"], v) for v in dump]
    if kind == "dict" and isinstance(dump, dict):
        return {
            k: _rebuilt_as_a_client_does(get_args(tp)[1], ref["value"], v) for k, v in dump.items()
        }
    return dump


def _is_rebuildable(
    annotation: Any, ref: dict[str, Any], adapter: Any, default: Any, dump: Any
) -> bool:
    """Whether a client can rebuild `default` from `dump` and put the same value on the wire.

    `dump` is the JSON-mode dump the description carries. The default is rebuilt exactly as a
    generated client builds it, with the model classes the service runs, where everything that can
    disagree is visible: an alias, a field or model serializer that changes a type, a union whose
    JSON form fits more than one member, `extra="allow"`, a masked secret, a validator that is not
    idempotent. It is true only when nothing disagreed: the rebuilt value equals the service's
    own, and the parameter's adapter dumps it, as the client's `_encode` does, to the same JSON
    values (so the order of keys does not matter).
    """
    try:
        rebuilt = _rebuilt_as_a_client_does(annotation, ref, dump)
        return bool(rebuilt == default) and canonical(
            adapter.dump_python(rebuilt, mode="json")
        ) == canonical(dump)
    except Exception:
        return False


@dataclass(frozen=True)
class Method:
    name: str
    doc: str | None
    params: list[Param]
    returns: dict[str, Any]
    signature_hash: str = ""
    doc_summary: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "doc": self.doc,
            "params": [p.to_dict() for p in self.params],
            "returns": self.returns,
            "signature_hash": self.signature_hash,
            "doc_summary": self.doc_summary,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Method:
        return cls(
            name=d["name"],
            doc=d.get("doc"),
            params=[Param.from_dict(p) for p in d.get("params", [])],
            returns=d["returns"],
            signature_hash=d.get("signature_hash", ""),
            doc_summary=d.get("doc_summary"),
            description=d.get("description"),
        )


def _signature_hash(params: list[Param], returns: dict[str, Any]) -> str:
    return _sha({"params": [p.to_dict() for p in params], "returns": returns})


def _param_of(spec: ParamSpec) -> Param:
    if not spec.has_default:
        return Param(name=spec.name, type=spec.ref)
    dump = spec.adapter.dump_python(spec.default, mode="json")
    holds_a_model = _holds_a_model(spec.ref)
    return Param(
        name=spec.name,
        type=spec.ref,
        has_default=True,
        default=dump,
        rebuildable=(
            _is_rebuildable(spec.annotation, spec.ref, spec.adapter, spec.default, dump)
            if holds_a_model
            else None
        ),
    )


# What a listener's description says that depends on the configuration it is described under: the
# subject it subscribes to, and the durable and queue group, which JetStream being on or off
# decides. The description hash leaves them out so it is the same offline and from a running
# service.
_CONFIG_DEPENDENT_LISTENER_KEYS = frozenset({"effective_subject", "durable", "queue_group"})


def _description_hash(
    methods: list[Method],
    listeners: list[EventListenerDescription],
    components: dict[str, Any],
    outputs: list[OutputDescription],
) -> str:
    """The hash of the whole description: everything in it that the configuration does not decide.

    The methods, the listeners' subjects, schemas and delivery kind, the models, and the outputs.
    The streams are left out, with the service name and version, because the configuration
    supplies them, and a hash that moved with the configuration would differ between the offline
    description and the running service's.
    """
    return _sha(
        {
            "methods": [m.to_dict() for m in methods],
            "listeners": [
                {
                    k: v
                    for k, v in item.to_dict().items()
                    if k not in _CONFIG_DEPENDENT_LISTENER_KEYS
                }
                for item in listeners
            ],
            "components": components,
            "outputs": [o.to_dict() for o in outputs],
        }
    )


@dataclass(frozen=True)
class EventListenerDescription:
    """Introspected description of an event listener or broadcast handler."""

    pattern: str = ""
    handler_name: str = ""
    schema: dict[str, Any] | None = None
    durable: str | None = None
    fanout: bool = False
    pull: bool = False
    queue_group: str | None = None
    doc: str | None = None
    doc_summary: str | None = None
    description: str | None = None
    cross_namespace: bool = False
    effective_subject: str | None = None

    @property
    def subject(self) -> str:
        """The pattern, under the name earlier descriptions gave it."""
        return self.pattern

    @property
    def handler(self) -> str:
        """The handler name, under the name earlier descriptions gave it."""
        return self.handler_name

    @property
    def is_validated(self) -> bool:
        """Whether the payload is a declared model: the description carries its schema."""
        return self.schema is not None

    @property
    def is_broadcast(self) -> bool:
        """Whether every replica handles each message, which is what `fanout` says."""
        return self.fanout

    @property
    def model_schema_hash(self) -> str | None:
        """The hash of the payload model's schema, or None for a listener with no model."""
        return self.schema.get("schema_hash") if isinstance(self.schema, dict) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "handler_name": self.handler_name,
            "schema": self.schema,
            "durable": self.durable,
            "fanout": self.fanout,
            "pull": self.pull,
            "queue_group": self.queue_group,
            "cross_namespace": self.cross_namespace,
            "effective_subject": self.effective_subject,
            "doc": self.doc,
            "doc_summary": self.doc_summary,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EventListenerDescription:
        return cls(
            pattern=d.get("pattern", d.get("subject", "")),
            handler_name=d.get("handler_name", d.get("handler", "")),
            schema=d.get("schema"),
            durable=d.get("durable"),
            fanout=d.get("fanout", d.get("is_broadcast", False)),
            pull=d.get("pull", False),
            queue_group=d.get("queue_group"),
            doc=d.get("doc"),
            doc_summary=d.get("doc_summary"),
            description=d.get("description"),
            cross_namespace=d.get("cross_namespace", False),
            effective_subject=d.get("effective_subject"),
        )


@dataclass(frozen=True)
class StreamDescription:
    """Introspected description of a JetStream stream topology."""

    name: str
    subjects: list[str] = field(default_factory=list)
    storage: str = "file"
    retention: str = "limits"
    max_age_seconds: float | None = None
    duplicate_window_seconds: float = 120.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "subjects": list(self.subjects),
            "storage": self.storage,
            "retention": self.retention,
            "max_age_seconds": self.max_age_seconds,
            "duplicate_window_seconds": self.duplicate_window_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StreamDescription:
        return cls(
            name=d["name"],
            subjects=list(d.get("subjects", [])),
            storage=d.get("storage", "file"),
            retention=d.get("retention", "limits"),
            max_age_seconds=d.get("max_age_seconds"),
            duplicate_window_seconds=d.get("duplicate_window_seconds", 120.0),
        )


@dataclass(frozen=True)
class Description:
    service: str
    version: str
    methods: list[Method] = field(default_factory=list)
    description_hash: str = ""
    components: dict[str, Any] = field(default_factory=dict)
    listeners: list[EventListenerDescription] = field(default_factory=list)
    streams: list[StreamDescription] = field(default_factory=list)
    outputs: list[OutputDescription] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "version": self.version,
            "methods": [m.to_dict() for m in self.methods],
            "description_hash": self.description_hash,
            "components": self.components,
            "listeners": [listener_desc.to_dict() for listener_desc in self.listeners],
            "streams": [s.to_dict() for s in self.streams],
            "outputs": [output.to_dict() for output in self.outputs],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Description:
        return cls(
            service=d["service"],
            version=d["version"],
            methods=[Method.from_dict(m) for m in d.get("methods", [])],
            description_hash=d.get("description_hash", ""),
            components=d.get("components", {}),
            listeners=[
                EventListenerDescription.from_dict(listener_desc)
                for listener_desc in d.get("listeners", [])
            ],
            streams=[StreamDescription.from_dict(s) for s in d.get("streams", [])],
            outputs=[OutputDescription.from_dict(output) for output in d.get("outputs", [])],
        )

    def method(self, name: str) -> Method | None:
        return next((m for m in self.methods if m.name == name), None)

    def listener(self, pattern: str) -> EventListenerDescription | None:
        return next(
            (listener_desc for listener_desc in self.listeners if listener_desc.pattern == pattern),
            None,
        )

    def stream(self, name: str) -> StreamDescription | None:
        return next((s for s in self.streams if s.name == name), None)


def describe(
    cls: type,
    service: str | None = None,
    version: str | None = None,
    *,
    config: Any = None,
) -> Description:
    """Walk the CLASS the way discovery does and describe every @rpc handler, listener, and stream.

    The same walk as `_discover_handlers`: public names, markers read from the
    function's own `__dict__` so a property is never evaluated, and the class
    rather than an instance so nothing a service assigns to `self` can appear.
    An unannotated handler raises `UntypedHandler` here for the same reason the
    service refuses to start -- a contract that cannot be read cannot be
    published either -- and that holds for event handlers as well as `@rpc`. A
    class the service would refuse is refused too: a handler decorated on an
    underscore-prefixed name or named for a `CliffracerService` method, two listeners
    on one subject, two subjects sharing a durable, a listener with neither a durable
    nor fanout, a pull listener with no durable or with fanout, and, when a
    `config` is given, a durable together with fanout on a JetStream service, a
    durable on a service without JetStream that is not fanout, a pull listener on a
    service without JetStream, or a `cross_namespace` listener on a service with no
    namespace. The rules that need the configuration are applied only when there is
    one to apply them to.

    Given a `config`, a listener's `queue_group` is the queue the runtime subscribes with, which
    is the durable under the config's `subject_prefix`; without one it is the declared durable.

    Given a `config` that leaves JetStream off, the description is of what the service
    would run: no streams, since none is created, and no durable or queue group on a
    listener, since the subscription has neither.

    The description carries what the source says: parameter defaults, docstrings, and a
    model's field defaults, descriptions and docstring. It is published to whoever the
    service answers `{service}.describe` for, so a value that must not leave the service is
    not a default and a maintainers' note is not a docstring.
    """
    svc_name = service or getattr(config, "name", None) or cls.__name__.lower()
    ver = (
        version
        or getattr(config, "version", None)
        # What a running service reports when its config sets no version, so a
        # class described without one matches the service it describes.
        or ServiceConfig.model_fields["version"].default
    )
    cfg = config
    # With a config that leaves JetStream off the runtime subscribes to a durable listener's
    # subject with no durable and no queue group, so that is what the description says.
    jetstream_off = cfg is not None and not cfg.jetstream_enabled

    def published_durable(durable: str | None) -> str | None:
        return None if jetstream_off else durable

    def queue_group_for(durable: str | None, is_pull: bool, is_fanout: bool) -> str | None:
        """The queue group the runtime subscribes with: the durable, under the config's prefix.

        Only a push durable on a JetStream service has one; a pull consumer, a fanout listener and
        a core subscription have none. With no config the prefix is unknown, so the declared name.
        """
        name = published_durable(durable)
        if not name or is_pull or is_fanout:
            return None
        return cfg.prefixed_name(name) if cfg is not None else name

    def effective_subject(pattern: str, cross_namespace: bool) -> str | None:
        """The subject the runtime subscribes to, or None when no config says where."""
        if cfg is None:
            return None
        return HandlerDiscovery.effective_event_subject(cfg, pattern, cross_namespace)

    methods: list[Method] = []
    components: dict[str, Any] = {}
    listeners: list[EventListenerDescription] = []
    streams: list[StreamDescription] = []
    # What the walk declares, in the shape discovery's own validators read, so the same
    # refusals can be applied to it. Keyed by the subject the runtime subscribes to, or by the
    # pattern when no config says where that is.
    declared = ServiceRegistry()

    def declare(
        key: str,
        handler_name: str,
        *,
        durable: str | None,
        fanout: bool,
        pull: bool,
        pause_when_down: tuple[str, ...] = (),
    ) -> None:
        if key in declared.event_handlers:
            previous = declared.event_handler_names.get(key, "unknown")
            raise ConfigurationError(
                f"Duplicate event listener declared on subject {key!r}: "
                f"{handler_name!r} conflicts with {previous!r}"
            )
        declared.event_handlers[key] = None  # type: ignore[assignment]
        declared.event_handler_names[key] = handler_name
        if durable:
            declared.event_durables[key] = durable
        if fanout:
            declared.event_fanout.add(key)
        if pull:
            declared.event_pull.add(key)
        if pause_when_down:
            declared.event_pause_when_down[key] = pause_when_down

    def refuse_cross_namespace_without_a_namespace(
        handler_name: str, pattern: str, cross_patterns: set[str]
    ) -> None:
        """Discovery's refusal of a cross-namespace listener on a service with no namespace,
        which needs the configuration to know whether there is one."""
        if cfg is not None and pattern in cross_patterns:
            HandlerDiscovery._refuse_cross_namespace_without_a_namespace(
                cls, cfg, handler_name, pattern
            )

    def key_for(pattern: str, cross_namespace: bool) -> str:
        return effective_subject(pattern, cross_namespace) or (
            f"*.{pattern}" if cross_namespace else pattern
        )

    for name, member in inspect.getmembers(cls):
        if name.startswith("_"):
            HandlerDiscovery._refuse_a_private_handler(cls, name, member)
            continue

        markers = getattr(member, "__dict__", None)
        if markers and any(
            key.startswith(HandlerDiscovery.HANDLER_MARKER_PREFIX) for key in markers
        ):
            HandlerDiscovery._refuse_a_handler_that_replaces_a_framework_method(cls, name, member)

        # Discover RPC handlers
        if not name.startswith("_") and markers and "_cliffracer_rpc" in markers:
            spec = build_handler_spec(name, member, owner=cls)
            collect_model_schemas(spec, components)
            params = [_param_of(p) for p in spec.params]
            methods.append(
                Method(
                    name=name,
                    doc=spec.doc,
                    params=params,
                    returns=spec.return_ref,
                    signature_hash=_signature_hash(params, spec.return_ref),
                    doc_summary=spec.doc_summary,
                    description=spec.doc_description,
                )
            )

        # Discover event handlers (_cliffracer_events)
        events = getattr(member, "_cliffracer_events", None)
        if events:
            build_event_spec(name, member, owner=cls)
            durables: dict[str, str] = getattr(member, "_cliffracer_event_durables", {})
            fanout_set: set[str] = getattr(member, "_cliffracer_event_fanout", set())
            pull_set: set[str] = getattr(member, "_cliffracer_event_pull", set())
            cross_set: set[str] = getattr(member, "_cliffracer_event_cross_namespace", set())
            raw_doc = inspect.getdoc(member)
            doc_summary = (
                next((line.strip() for line in raw_doc.splitlines() if line.strip()), None)
                if raw_doc
                else None
            )
            for pattern in events:
                refuse_cross_namespace_without_a_namespace(name, pattern, cross_set)
                is_fanout = pattern in fanout_set
                is_pull = pattern in pull_set
                durable_name = durables.get(pattern)
                declare(
                    key_for(pattern, pattern in cross_set),
                    name,
                    durable=durable_name,
                    fanout=is_fanout,
                    pull=is_pull,
                    pause_when_down=getattr(member, "_cliffracer_event_pause_when_down", {}).get(
                        pattern, ()
                    ),
                )
                queue_group = queue_group_for(durable_name, is_pull, is_fanout)
                listeners.append(
                    EventListenerDescription(
                        pattern=pattern,
                        handler_name=name,
                        schema=None,
                        durable=published_durable(durable_name),
                        fanout=is_fanout,
                        pull=is_pull,
                        cross_namespace=pattern in cross_set,
                        effective_subject=effective_subject(pattern, pattern in cross_set),
                        queue_group=queue_group,
                        doc=doc_summary,
                        doc_summary=doc_summary,
                        description=raw_doc,
                    )
                )

        # Discover validated event handlers (_cliffracer_validated_events)
        val_events = getattr(member, "_cliffracer_validated_events", None)
        if val_events:
            v_durables: dict[str, str] = getattr(member, "_cliffracer_event_durables", {})
            v_fanout_set: set[str] = getattr(member, "_cliffracer_event_fanout", set())
            v_cross_set: set[str] = getattr(member, "_cliffracer_event_cross_namespace", set())
            raw_doc = inspect.getdoc(member)
            doc_summary = (
                next((line.strip() for line in raw_doc.splitlines() if line.strip()), None)
                if raw_doc
                else None
            )
            for pattern, schema_cls, _on_invalid in val_events:
                build_validated_event_spec(name, member, owner=cls, schema=schema_cls)
                refuse_cross_namespace_without_a_namespace(name, pattern, v_cross_set)
                is_fanout = pattern in v_fanout_set
                # `validated_listener` has no `pull` option, so it never marks one.
                is_pull = False
                durable_name = v_durables.get(pattern)
                declare(
                    key_for(pattern, pattern in v_cross_set),
                    name,
                    durable=durable_name,
                    fanout=is_fanout,
                    pull=is_pull,
                    pause_when_down=getattr(member, "_cliffracer_event_pause_when_down", {}).get(
                        pattern, ()
                    ),
                )
                queue_group = queue_group_for(durable_name, is_pull, is_fanout)
                schema_ref = type_ref(schema_cls)
                collect_model_schemas(schema_cls, components)
                listeners.append(
                    EventListenerDescription(
                        pattern=pattern,
                        handler_name=name,
                        schema=schema_ref,
                        durable=published_durable(durable_name),
                        fanout=is_fanout,
                        pull=is_pull,
                        cross_namespace=pattern in v_cross_set,
                        effective_subject=effective_subject(pattern, pattern in v_cross_set),
                        queue_group=queue_group,
                        doc=doc_summary,
                        doc_summary=doc_summary,
                        description=raw_doc,
                    )
                )

        # Discover broadcast handlers (_cliffracer_broadcast)
        bcast_pattern = getattr(member, "_cliffracer_broadcast", None)
        if bcast_pattern:
            build_event_spec(name, member, owner=cls)
            declare(key_for(bcast_pattern, False), name, durable=None, fanout=True, pull=False)
            raw_doc = inspect.getdoc(member)
            doc_summary = (
                next((line.strip() for line in raw_doc.splitlines() if line.strip()), None)
                if raw_doc
                else None
            )
            listeners.append(
                EventListenerDescription(
                    pattern=bcast_pattern,
                    handler_name=name,
                    schema=None,
                    durable=None,
                    fanout=True,
                    pull=False,
                    effective_subject=effective_subject(bcast_pattern, False),
                    queue_group=None,
                    doc=doc_summary,
                    doc_summary=doc_summary,
                    description=raw_doc,
                )
            )

    # The refusals discovery applies to the whole class, over what the walk declared. The pull
    # rule about JetStream and the durable-with-fanout rule need the configuration; the pull
    # rules about a missing durable and about fanout do not.
    HandlerDiscovery.validate_pull_is_usable(
        declared,
        cfg if cfg is not None else SimpleNamespace(jetstream_enabled=True),  # type: ignore[arg-type]
    )
    if cfg is not None:
        HandlerDiscovery.validate_fanout_and_durable_are_exclusive(declared, cfg)
    # Offline, JetStream is taken to be on, so a durable counts as declared and only a listener
    # with neither a durable nor fanout is refused; the configuration decides the rest.
    HandlerDiscovery.validate_fanout_declared(
        declared,
        cfg if cfg is not None else SimpleNamespace(jetstream_enabled=True, name=svc_name),  # type: ignore[arg-type]
    )
    HandlerDiscovery.validate_unique_durables(declared)
    # Whether each name is a declared dependency is not checked offline: a dependency the service
    # adds with `add_dependency` is not in the class. The shape is.
    HandlerDiscovery.validate_pause_when_down_is_durable(
        declared,
        cfg if cfg is not None else SimpleNamespace(jetstream_enabled=True),  # type: ignore[arg-type]
    )

    # Discover streams from config. The runtime provisions them only when JetStream is on.
    if cfg is not None and cfg.jetstream_enabled:
        declared_streams = getattr(cfg, "effective_jetstream_streams", None) or []
        for s in declared_streams:
            streams.append(
                StreamDescription(
                    name=s.name,
                    subjects=list(s.subjects),
                    storage=s.storage,
                    retention=s.retention,
                    max_age_seconds=s.max_age_seconds,
                    duplicate_window_seconds=s.effective_duplicate_window(),
                )
            )

    methods.sort(key=lambda m: m.name)
    listeners.sort(key=lambda item: (item.pattern, item.handler_name))
    streams.sort(key=lambda s: s.name)
    outputs = list(describe_outputs(cls))

    return Description(
        service=svc_name,
        version=ver,
        methods=methods,
        description_hash=_description_hash(methods, listeners, components, outputs),
        components=components,
        listeners=listeners,
        streams=streams,
        outputs=outputs,
    )

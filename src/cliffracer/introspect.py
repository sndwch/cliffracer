"""Describe service classes, RPC signatures, and interface hashes for client generation."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from typing import Any

from cliffracer.core.typed_rpc import build_handler_spec, collect_model_schemas, type_ref


def canonical(obj: Any) -> str:
    """The one serialisation the hashes are taken over.

    Sorted keys and no whitespace, so two processes that agree about the
    content agree about the bytes.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj).encode()).hexdigest()


@dataclass(frozen=True)
class Param:
    name: str
    type: dict[str, Any]
    has_default: bool = False
    default: Any = None

    def to_dict(self) -> dict[str, Any]:
        # `default` is ABSENT rather than null when there is none: a parameter
        # defaulting to None and one with no default are different things to a
        # generated client, and null cannot tell them apart.
        d: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.has_default:
            d["default"] = self.default
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Param:
        return cls(
            name=d["name"],
            type=d["type"],
            has_default="default" in d,
            default=d.get("default"),
        )


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
    stream: str | None = None
    subject: str = ""
    handler: str = ""
    is_validated: bool = False
    is_broadcast: bool = False
    model_schema_hash: str | None = None

    def __post_init__(self) -> None:
        pat = self.pattern or self.subject
        h_name = self.handler_name or self.handler
        is_val = self.is_validated or (self.schema is not None)
        is_bcast = self.is_broadcast or self.fanout
        m_hash = self.model_schema_hash or (
            self.schema.get("schema_hash") if isinstance(self.schema, dict) else None
        )
        object.__setattr__(self, "pattern", pat)
        object.__setattr__(self, "subject", pat)
        object.__setattr__(self, "handler_name", h_name)
        object.__setattr__(self, "handler", h_name)
        object.__setattr__(self, "is_validated", is_val)
        object.__setattr__(self, "is_broadcast", is_bcast)
        object.__setattr__(self, "fanout", is_bcast or self.fanout)
        object.__setattr__(self, "model_schema_hash", m_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "handler_name": self.handler_name,
            "schema": self.schema,
            "durable": self.durable,
            "fanout": self.fanout,
            "pull": self.pull,
            "queue_group": self.queue_group,
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
            stream=d.get("stream"),
        )


@dataclass(frozen=True)
class StreamDescription:
    """Introspected description of a JetStream stream topology."""

    name: str
    subjects: list[str] = field(default_factory=list)
    storage: str = "file"
    retention: str = "limits"
    max_age_seconds: float | None = None
    max_consumers: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "subjects": list(self.subjects),
            "storage": self.storage,
            "retention": self.retention,
            "max_age_seconds": self.max_age_seconds,
        }
        if self.max_consumers is not None:
            d["max_consumers"] = self.max_consumers
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StreamDescription:
        return cls(
            name=d["name"],
            subjects=list(d.get("subjects", [])),
            storage=d.get("storage", "file"),
            retention=d.get("retention", "limits"),
            max_age_seconds=d.get("max_age_seconds"),
            max_consumers=d.get("max_consumers"),
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "version": self.version,
            "methods": [m.to_dict() for m in self.methods],
            "description_hash": self.description_hash,
            "components": self.components,
            "listeners": [listener_desc.to_dict() for listener_desc in self.listeners],
            "streams": [s.to_dict() for s in self.streams],
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
        )

    def method(self, name: str) -> Method | None:
        return next((m for m in self.methods if m.name == name), None)

    def listener(self, pattern: str) -> EventListenerDescription | None:
        return next(
            (
                listener_desc
                for listener_desc in self.listeners
                if listener_desc.pattern == pattern or listener_desc.subject == pattern
            ),
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
    **kwargs: Any,
) -> Description:
    """Walk the CLASS the way discovery does and describe every @rpc handler, listener, and stream.

    The same walk as `_discover_handlers`: public names, markers read from the
    function's own `__dict__` so a property is never evaluated, and the class
    rather than an instance so nothing a service assigns to `self` can appear.
    An unannotated handler raises `UntypedHandler` here for the same reason the
    service refuses to start -- a contract that cannot be read cannot be
    published either.
    """
    svc_name = (
        service
        or kwargs.get("service")
        or getattr(config, "name", None)
        or getattr(cls, "service_name", None)
        or getattr(cls, "__name__", "service").lower()
    )
    ver = (
        version
        or kwargs.get("version")
        or getattr(config, "version", None)
        or getattr(cls, "version", None)
        or "1.0.0"
    )
    cfg = config or kwargs.get("config")

    methods: list[Method] = []
    components: dict[str, Any] = {}
    listeners: list[EventListenerDescription] = []
    streams: list[StreamDescription] = []

    for name, member in inspect.getmembers(cls):
        if name.startswith("__"):
            continue

        markers = getattr(member, "__dict__", None)

        # Discover RPC handlers
        if not name.startswith("_") and markers and "_cliffracer_rpc" in markers:
            spec = build_handler_spec(name, member, owner=cls)
            collect_model_schemas(spec, components)
            params = [
                Param(
                    name=p.name,
                    type=p.ref,
                    has_default=p.has_default,
                    default=p.adapter.dump_python(p.default, mode="json")
                    if p.has_default
                    else None,
                )
                for p in spec.params
            ]
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
            durables: dict[str, str] = getattr(member, "_cliffracer_event_durables", {})
            fanout_set: set[str] = getattr(member, "_cliffracer_event_fanout", set())
            pull_set: set[str] = getattr(member, "_cliffracer_event_pull", set())
            raw_doc = inspect.getdoc(member)
            doc_summary = (
                next((line.strip() for line in raw_doc.splitlines() if line.strip()), None)
                if raw_doc
                else None
            )
            for pattern in events:
                is_fanout = pattern in fanout_set
                is_pull = pattern in pull_set
                durable_name = durables.get(pattern)
                queue_group = (
                    durable_name if (durable_name and not is_pull and not is_fanout) else None
                )
                listeners.append(
                    EventListenerDescription(
                        pattern=pattern,
                        handler_name=name,
                        schema=None,
                        durable=durable_name,
                        fanout=is_fanout,
                        pull=is_pull,
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
            v_pull_set: set[str] = getattr(member, "_cliffracer_event_pull", set())
            raw_doc = inspect.getdoc(member)
            doc_summary = (
                next((line.strip() for line in raw_doc.splitlines() if line.strip()), None)
                if raw_doc
                else None
            )
            for pattern, schema_cls, _on_invalid in val_events:
                is_fanout = pattern in v_fanout_set
                is_pull = pattern in v_pull_set
                durable_name = v_durables.get(pattern)
                queue_group = (
                    durable_name if (durable_name and not is_pull and not is_fanout) else None
                )
                schema_ref = type_ref(schema_cls)
                collect_model_schemas(schema_cls, components)
                listeners.append(
                    EventListenerDescription(
                        pattern=pattern,
                        handler_name=name,
                        schema=schema_ref,
                        durable=durable_name,
                        fanout=is_fanout,
                        pull=is_pull,
                        queue_group=queue_group,
                        doc=doc_summary,
                        doc_summary=doc_summary,
                        description=raw_doc,
                    )
                )

        # Discover broadcast handlers (_cliffracer_broadcast)
        bcast_pattern = getattr(member, "_cliffracer_broadcast", None)
        if bcast_pattern:
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
                    queue_group=None,
                    doc=doc_summary,
                    doc_summary=doc_summary,
                    description=raw_doc,
                )
            )

    # Discover streams from config
    if cfg is not None:
        declared_streams = getattr(cfg, "jetstream_streams", None) or []
        for s in declared_streams:
            streams.append(
                StreamDescription(
                    name=s.name,
                    subjects=list(s.subjects),
                    storage=getattr(s, "storage", "file"),
                    retention=getattr(s, "retention", "limits"),
                    max_age_seconds=getattr(s, "max_age_seconds", None),
                )
            )

    methods.sort(key=lambda m: m.name)
    listeners.sort(key=lambda item: (item.pattern, item.handler_name))
    streams.sort(key=lambda s: s.name)

    return Description(
        service=svc_name,
        version=ver,
        methods=methods,
        description_hash=_sha([m.to_dict() for m in methods]),
        components=components,
        listeners=listeners,
        streams=streams,
    )

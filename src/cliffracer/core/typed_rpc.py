"""Typed RPC: handler annotations define the request and response contract.

A handler's parameters and return are resolved from its type hints into a
structural TypeRef (never a Python repr string), validated with one pydantic
TypeAdapter each, and refused at service start when a type is outside the
supported set.

WHY STRUCTURAL RATHER THAN A REPR STRING. The TypeRef crosses the wire and is
turned back into a Python annotation by a generator on another machine. A
string like ``list[Order]`` would have to be parsed and its ``Order`` guessed
at; ``{"kind": "list", "item": {"kind": "model", "module": ..., "qualname":
...}}`` says which ``Order``, so the generated client imports the model from
the package that defines it instead of carrying a copy.
"""

from __future__ import annotations

import collections.abc
import enum
import functools
import hashlib
import inspect
import json
import keyword
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    PydanticInvalidForJsonSchema,
    PydanticUserError,
    TypeAdapter,
    ValidationError,
    create_model,
)
from pydantic.fields import FieldInfo

#: The JSON Schema mode a model is hashed in: what a caller may send, or what a handler writes.
SchemaMode = Literal["validation", "serialization"]

SCALARS: dict[type, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    type(None): "none",
}

_RESERVED_PARAM_NAMES = set(dir(BaseModel)) | {
    "__config__",
    "__base__",
    "__module__",
    "__validators__",
    "__cls_kwargs__",
    "model_config",
}


# Names an RPC handler parameter cannot take because a caller could not pass it. `call_rpc`,
# `call_async` and `call_rpc_no_wait` take the routing namespace as `namespace=` and collect the
# remote arguments as `**kwargs`, so a remote `namespace` argument collides with the routing
# one and the call fails with a `TypeError` that names an internal parameter. A caller going
# through `RpcProxy` has no other way to pass it.
_RPC_ROUTING_PARAM_NAMES = frozenset({"namespace"})


@functools.cache
def reserved_rpc_method_names() -> frozenset[str]:
    """Every public name a `ServiceClient` already has.

    A generated client subclasses `ServiceClient` and defines one method per
    handler, so a handler named after a class member replaces it, and one named
    after an attribute the constructor sets is shadowed by that attribute on
    every instance -- `client.connect_timeout` is then a float, and calling it
    raises. The names are read off the class and off a constructed client
    rather than listed, so an attribute added to the constructor is reserved
    with it. Imported here rather than at module level because `cliffracer.client`
    imports this module's dependents.
    """
    from cliffracer.client import ServiceClient

    # An empty prefix pins none: this client addresses nothing, and one given no prefix would
    # read `CLIFFRACER_SUBJECT_PREFIX`, so a bad value there would stop every service that
    # declares an RPC method from starting.
    client = ServiceClient(service="reserved", verify=False, subject_prefix="")
    names = set(dir(ServiceClient)) | set(vars(client))
    return frozenset(name for name in names if not name.startswith("_"))


# Bare containers and everything else that cannot describe its contents. Named
# explicitly so the refusal message can name them, rather than falling through
# to the generic branch and reporting a less useful error.
_BARE_OR_UNSUPPORTED = (list, dict, set, tuple, bytes, complex, object)


class UnsupportedType(TypeError):
    """A type the typed-RPC contract cannot express. Names the type."""


def _name(tp: Any) -> str:
    return getattr(tp, "__qualname__", None) or getattr(tp, "__name__", None) or repr(tp)


def _is_optional(tp: Any) -> tuple[bool, Any]:
    """``T | None`` and ``Optional[T]`` only.

    A union of two non-None types is NOT optional and is not supported: the
    client would have no rule for which branch to encode into.
    """
    origin = get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1 and len(get_args(tp)) == 2:
            return True, args[0]
    return False, None


def _valid_cid_annotation(tp: Any) -> bool:
    """Accept str, str | None, Optional[str], or Annotated thereof."""
    if get_origin(tp) is Annotated:
        tp = get_args(tp)[0]
    if tp is str:
        return True
    is_opt, inner = _is_optional(tp)
    return is_opt and inner is str


CONSTRAINT_ATTRS: tuple[str, ...] = (
    "ge",
    "le",
    "gt",
    "lt",
    "min_length",
    "max_length",
    "pattern",
    "strict",
    "multiple_of",
    "max_digits",
    "decimal_places",
    "allow_inf_nan",
    "coerce_numbers_to_str",
)


def extract_constraints(tp: Any) -> dict[str, Any]:
    """Extract validation constraints from an Annotated type's metadata."""
    if get_origin(tp) is not Annotated:
        return {}
    constraints: dict[str, Any] = {}
    for arg in get_args(tp)[1:]:
        items = getattr(arg, "metadata", None)
        items = items if items is not None else [arg]
        for item in items:
            for attr in CONSTRAINT_ATTRS:
                if hasattr(item, attr):
                    val = getattr(item, attr)
                    if val is not None:
                        if attr == "pattern" and hasattr(val, "pattern"):
                            val = val.pattern
                        constraints[attr] = val
    return dict(sorted(constraints.items()))


def type_ref(tp: Any, *, mode: SchemaMode = "validation") -> dict[str, Any]:
    """Annotation -> TypeRef. Raises UnsupportedType for anything else.

    A model's `schema_hash` is the hash of its JSON Schema in `mode`: `"validation"` for what a
    caller may send, `"serialization"` for what a handler returns (a `computed_field` or a
    `serialization_alias` changes what is written and not what is read).
    """
    if get_origin(tp) is Annotated:
        base = type_ref(get_args(tp)[0], mode=mode)
        constraints = extract_constraints(tp)
        if constraints:
            existing = base.get("constraints", {})
            merged = {**existing, **constraints}
            return {**base, "constraints": dict(sorted(merged.items()))}
        return base
    # `tp in SCALARS` before the bare-container check: bool is a subclass of
    # int and both are keys, so the dict lookup is exact and safe here.
    if tp in SCALARS:
        return {"kind": "scalar", "name": SCALARS[tp]}
    if tp is Any or tp in _BARE_OR_UNSUPPORTED:
        raise UnsupportedType(f"{_name(tp)} is unsupported; use a supported type")
    if inspect.isclass(tp) and issubclass(tp, BaseModel):
        # Model importability checks are deferred to client generation commands.
        schema = _model_json_schema(tp, mode)
        schema_hash = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]
        return {
            "kind": "model",
            "module": tp.__module__,
            "qualname": tp.__qualname__,
            "schema_hash": schema_hash,
        }
    is_opt, inner = _is_optional(tp)
    if is_opt:
        return {"kind": "optional", "inner": type_ref(inner, mode=mode)}
    origin = get_origin(tp)
    if origin is list:
        (item,) = get_args(tp)
        return {"kind": "list", "item": type_ref(item, mode=mode)}
    if origin is dict:
        key, value = get_args(tp)
        if key is not str:
            raise UnsupportedType(f"dict keys must be str, got {_name(key)}; this is unsupported")
        return {"kind": "dict", "value": type_ref(value, mode=mode)}
    if origin is Literal:
        raw_values = list(get_args(tp))
        if not raw_values:
            raise UnsupportedType(f"{_name(tp)} is unsupported; Literal cannot be empty")
        values = [v.value if isinstance(v, enum.Enum) else v for v in raw_values]
        if not all(
            isinstance(v, str | int | bool) and not isinstance(v, enum.Enum) for v in values
        ):
            raise UnsupportedType("Literal values must be str, int or bool; this is unsupported")
        return {"kind": "literal", "values": values}
    raise UnsupportedType(f"{_name(tp)} is unsupported")


@dataclass(frozen=True)
class ParamSpec:
    name: str
    annotation: Any
    ref: dict[str, Any]
    adapter: TypeAdapter
    has_default: bool
    default: Any = None


@dataclass(frozen=True)
class HandlerSpec:
    """Everything dispatch and introspection need about one RPC handler.

    payload_model is a pydantic model synthesised from the parameters with
    extra="forbid", so ONE model_validate call yields pydantic's own
    `missing`, `extra_forbidden` and field errors with the right `loc`; the
    dispatcher never hand-builds an error dict.
    """

    name: str
    params: list[ParamSpec]
    return_annotation: Any
    return_ref: dict[str, Any]
    #: What the handler returns, validated and dumped: the return type, or for a handler that
    #: streams its reply, the type of each item it yields.
    return_adapter: TypeAdapter
    takes_correlation_id: bool
    payload_model: type[BaseModel]
    doc: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    doc_summary: str | None = None
    doc_description: str | None = None
    #: Whether the handler is an async generator that streams its reply, one item at a time.
    streams: bool = False

    @property
    def description(self) -> str | None:
        return self.doc_description


def _model_json_schema(tp: type[BaseModel], mode: SchemaMode = "validation") -> dict[str, Any]:
    """The model's JSON Schema, or `UnsupportedType` naming the model that has none.

    A model with a field pydantic cannot describe (an arbitrary class, a callable) has no JSON
    Schema, and the contract is published as one. The refusal is the module's own, so the
    handler that takes or returns the model is named by whoever catches it.
    """
    try:
        return tp.model_json_schema(mode=mode)
    except PydanticInvalidForJsonSchema as exc:
        raise UnsupportedType(
            f"{tp.__qualname__} has no JSON Schema, so a contract cannot carry it: "
            f"{str(exc).splitlines()[0]}"
        ) from exc


def refuse_a_parameter_alias(qual: str, pname: str, annotation: Any) -> None:
    """Refuse a handler parameter that declares a Pydantic alias.

    A parameter is described, and called by a generated client or `RpcProxy`, under its Python
    name, while the payload model validates it under its alias, so a caller that follows the
    description is refused with `missing`. The alias has no use on a handler parameter, so it is
    refused when the handler is discovered (at start, and by `describe`), naming the parameter.
    """
    if get_origin(annotation) is not Annotated:
        return
    for meta in get_args(annotation)[1:]:
        if not isinstance(meta, FieldInfo):
            continue
        declared = {
            kind: getattr(meta, kind)
            for kind in ("alias", "validation_alias", "serialization_alias")
            if getattr(meta, kind) is not None
        }
        if declared:
            shown_aliases = ", ".join(f"{kind}={value!r}" for kind, value in declared.items())
            raise UntypedHandler(
                f"{qual}: parameter {pname!r} declares {shown_aliases}; a handler parameter is "
                f"described and called by its Python name, so an alias would make the description "
                f"and the handler disagree. Remove the alias"
            )


def collect_model_schemas(
    tp: Any, out: dict[str, Any] | None = None, mode: SchemaMode = "validation"
) -> dict[str, Any]:
    """Recursively collect Pydantic models into out keyed by schema_hash.

    A handler's parameters are collected in validation mode and its return in serialization
    mode, the modes `type_ref` hashes them in.
    """
    if out is None:
        out = {}
    if tp is None:
        return out
    if isinstance(tp, HandlerSpec):
        for p in tp.params:
            collect_model_schemas(p.annotation, out)
        collect_model_schemas(tp.return_annotation, out, mode="serialization")
        return out
    if isinstance(tp, ParamSpec):
        return collect_model_schemas(tp.annotation, out, mode)

    origin = get_origin(tp)
    if origin is Annotated:
        return collect_model_schemas(get_args(tp)[0], out, mode)
    if inspect.isclass(tp) and issubclass(tp, BaseModel):
        schema = _model_json_schema(tp, mode)
        schema_hash = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]
        if schema_hash not in out:
            out[schema_hash] = schema
            for field_info in tp.model_fields.values():
                collect_model_schemas(field_info.annotation, out, mode)
        return out
    if origin in (list, set, frozenset, tuple, dict):
        for arg in get_args(tp):
            collect_model_schemas(arg, out, mode)
        return out
    if origin is Union or origin is types.UnionType:
        for arg in get_args(tp):
            collect_model_schemas(arg, out, mode)
        return out
    if origin in _STREAM_ORIGINS:
        return collect_model_schemas(get_args(tp)[0], out, mode)
    return out


#: The annotations a handler that streams its reply declares: what an async generator returns.
_STREAM_ORIGINS = (collections.abc.AsyncIterator, collections.abc.AsyncGenerator)


def stream_item(tp: Any) -> Any:
    """The item type `tp` streams, for `AsyncIterator[X]` or `AsyncGenerator[X, None]` (under any
    `Annotated`), or None when `tp` does not stream."""
    while get_origin(tp) is Annotated:
        tp = get_args(tp)[0]
    if tp in _STREAM_ORIGINS:
        raise UnsupportedType(f"{_name(tp)} without an item type is unsupported")
    if get_origin(tp) not in _STREAM_ORIGINS:
        return None
    args = get_args(tp)
    if not args:
        raise UnsupportedType(f"{_name(tp)} without an item type is unsupported")
    # `collections.abc.AsyncGenerator[X, None]` holds the literal `None`, the `typing` spelling
    # `NoneType`: both mean no send type.
    if len(args) == 2 and args[1] not in (None, type(None)):
        raise UnsupportedType(
            f"{_name(tp)} with a send type is unsupported: a stream is only iterated"
        )
    return args[0]


def return_type_ref(tp: Any) -> dict[str, Any]:
    """A handler's return as a TypeRef: `{"kind": "stream", "item": ...}` for an annotation that
    streams, else what `type_ref` makes of it. A stream is a handler's whole return, so `type_ref`
    itself refuses one nested in another type, and the item is read as a return is."""
    item = stream_item(tp)
    if item is None:
        return type_ref(tp, mode="serialization")
    return {"kind": "stream", "item": type_ref(item, mode="serialization")}


class UntypedHandler(TypeError):
    """An @rpc handler that is not fully annotated. The service refuses to start."""


def require_finite_json(qual: str, what: str, publish: Callable[[], Any]) -> None:
    """Refuse a part of a contract that the published description could not carry.

    The description is published as JSON, and JSON has no `inf`, `-inf` or `nan`: a parser in
    another language rejects the whole document, so one such default would make every client
    unable to read the service. `publish` builds the value the description carries; an error
    that building it raises is reported by `describe`, which builds it again.
    """
    try:
        value = publish()
    except Exception:  # noqa: BLE001 - not this check's finding; describe raises it
        return
    try:
        json.dumps(value, allow_nan=False)
    except ValueError as exc:
        raise UntypedHandler(
            f"{qual}: {what} holds a number that is not finite (inf, -inf or nan), which JSON "
            f"cannot carry and the service's description is published as JSON. Use None for "
            f"'no limit' (`float | None = None`), or leave the bound out: {exc}"
        ) from exc


def build_handler_spec(name: str, func: Callable, *, owner: type) -> HandlerSpec:
    """Read one handler's contract from its signature, or refuse by name.

    Every refusal names `Owner.handler` and the offending parameter or the
    return, because this fires at service start and the operator reading it
    has a whole class of handlers to choose between.
    """
    qual = f"{owner.__qualname__}.{name}"
    if name in reserved_rpc_method_names():
        raise UntypedHandler(
            f"{qual}: RPC handler name {name!r} conflicts with ServiceClient member; choose a different name"
        )
    # A decorator that keeps `__wrapped__` (`functools.wraps`) hides what the handler is; the
    # function it wraps is what a call reaches.
    target = inspect.unwrap(func)
    if inspect.isgeneratorfunction(target):
        raise UntypedHandler(
            f"{qual}: an RPC handler cannot be a generator: calling it only builds the "
            "generator, so its body would never run and no reply would carry what it yields"
        )
    is_async_generator = inspect.isasyncgenfunction(target)
    try:
        hints = typing.get_type_hints(func, include_extras=True)
    except Exception as exc:  # noqa: BLE001 - a hint that cannot resolve is an untyped handler
        raise UntypedHandler(f"{qual}: type hints do not resolve: {exc}") from exc
    sig = inspect.signature(func)
    params: list[ParamSpec] = []
    takes_cid = False
    is_static = isinstance(func, staticmethod) or isinstance(
        inspect.getattr_static(owner, name, None), staticmethod
    )
    for idx, (pname, p) in enumerate(sig.parameters.items()):
        if idx == 0 and pname == "self" and not is_static:
            continue
        if p.kind is p.POSITIONAL_ONLY:
            raise UntypedHandler(
                f"{qual}: positional-only parameter {pname!r} is not allowed on an RPC handler"
            )
        if pname == "correlation_id":
            takes_cid = True
            if pname in hints and not _valid_cid_annotation(hints[pname]):
                raise UntypedHandler(
                    f"{qual}: correlation_id annotation must be str or str | None, got {_name(hints[pname])}"
                )
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise UntypedHandler(f"{qual}: *{pname} is not allowed on an RPC handler")
        if pname.startswith("_"):
            raise UntypedHandler(
                f"{qual}: parameter {pname!r} starting with '_' cannot be a valid RPC parameter"
            )
        if pname in _RESERVED_PARAM_NAMES:
            raise UntypedHandler(
                f"{qual}: parameter {pname!r} conflicts with BaseModel member; choose a different name"
            )
        if pname in _RPC_ROUTING_PARAM_NAMES:
            raise UntypedHandler(
                f"{qual}: parameter {pname!r} is the routing argument of call_rpc and "
                f"call_async, so a caller using RpcProxy could not pass it; choose a different name"
            )
        if pname not in hints:
            raise UntypedHandler(f"{qual}: parameter {pname!r} has no annotation")
        refuse_a_parameter_alias(qual, pname, hints[pname])
        try:
            ref = type_ref(hints[pname])
        except UnsupportedType as exc:
            raise UntypedHandler(f"{qual}: parameter {pname!r}: {exc}") from exc
        has_default = p.default is not inspect.Parameter.empty
        adapter = TypeAdapter(hints[pname])
        if has_default:
            try:
                adapter.validate_python(p.default)
            except Exception as exc:
                raise UntypedHandler(
                    f"{qual}: default value {p.default!r} for parameter {pname!r} does not match annotation: {exc}"
                ) from exc
        params.append(
            ParamSpec(
                name=pname,
                annotation=hints[pname],
                ref=ref,
                adapter=adapter,
                has_default=has_default,
                default=p.default if has_default else None,
            )
        )
    if "return" not in hints:
        raise UntypedHandler(f"{qual}: the return has no annotation")
    try:
        streamed = stream_item(hints["return"])
    except UnsupportedType as exc:
        raise UntypedHandler(f"{qual}: return: {exc}") from exc
    if streamed is not None and not is_async_generator:
        raise UntypedHandler(
            f"{qual}: return: annotated {_name(hints['return'])}, a stream, but the handler is not "
            "an async generator; a handler that streams its reply yields each item"
        )
    if is_async_generator and streamed is None:
        raise UntypedHandler(
            f"{qual}: an RPC handler cannot be a generator unless it streams its reply: calling "
            "it only builds the generator, so annotate its return AsyncIterator[X] to send each "
            "item it yields"
        )
    if streamed is not None and getattr(func, "_cliffracer_async_rpc", False):
        raise UntypedHandler(
            f"{qual}: a handler that streams its reply cannot be @async_rpc: a fire-and-forget "
            "call has nobody to stream to"
        )
    try:
        return_ref = return_type_ref(hints["return"])
    except UnsupportedType as exc:
        raise UntypedHandler(f"{qual}: return: {exc}") from exc
    raw_doc = inspect.getdoc(func)
    if raw_doc:
        lines = [line.strip() for line in raw_doc.splitlines()]
        doc_summary = next((line for line in lines if line), None)
        doc_description = raw_doc
    else:
        doc_summary = None
        doc_description = None

    for ps in params:
        where = f"parameter {ps.name!r}"
        require_finite_json(qual, f"the type of {where}", functools.partial(dict, ps.ref))
        if ps.has_default:
            require_finite_json(
                qual,
                f"the default of {where}",
                functools.partial(ps.adapter.dump_python, ps.default, mode="json"),
            )
        require_finite_json(
            qual, f"a model in {where}", functools.partial(collect_model_schemas, ps.annotation)
        )
    require_finite_json(qual, "the return type", functools.partial(dict, return_ref))
    require_finite_json(
        qual,
        "a model in the return type",
        functools.partial(collect_model_schemas, hints["return"], mode="serialization"),
    )

    fields = {p.name: (p.annotation, p.default if p.has_default else ...) for p in params}
    try:
        payload_model = create_model(  # type: ignore[call-overload]
            f"{owner.__name__}_{name}_Payload",
            __config__=ConfigDict(extra="forbid", validate_default=True),
            **fields,
        )
    except (ValidationError, PydanticUserError) as exc:
        raise UntypedHandler(f"{qual}: payload model creation failed: {exc}") from exc
    return HandlerSpec(
        name=name,
        params=params,
        return_annotation=hints["return"],
        return_ref=return_ref,
        return_adapter=TypeAdapter(hints["return"] if streamed is None else streamed),
        takes_correlation_id=takes_cid,
        payload_model=payload_model,
        doc=doc_summary,
        doc_summary=doc_summary,
        doc_description=doc_description,
        streams=streamed is not None,
    )


def shown(text: Any) -> str:
    """`text` as one printable line: control characters and line breaks are escaped, not echoed."""
    text = str(text)
    return text if text.isprintable() else ascii(text)[1:-1]


def _is_a_dotted_identifier_path(text: Any) -> bool:
    """Whether `text` is a dotted path of plain identifiers, none of them a keyword."""
    return isinstance(text, str) and all(
        part.isidentifier() and not keyword.iskeyword(part) for part in text.split(".")
    )


def unimportable_models(ref: dict[str, Any]) -> list[str]:
    """``"module:qualname"`` for every model in a TypeRef a client cannot import.

    Refuse models whose module is private or `__main__`, or is not a dotted path of plain
    identifiers, and whose qualname is not one, because the generated file writes the module and
    the qualname into an `import` line and a name. A module that is anything else is text from
    whoever answered `describe`, and written into the file it is code that runs when the file is
    imported. A service with such a model runs perfectly well; only a client generated from it
    would not import.

    One compact identifier per model, not a sentence, because the command joins
    them into a single line and adds the remedy once. Control characters in the text are escaped,
    so a refusal cannot be made to print them.
    """
    kind = ref["kind"]
    if kind == "model":
        module = ref["module"]
        qualname = ref["qualname"]
        if (
            not _is_a_dotted_identifier_path(module)
            or module == "__main__"
            or any(part.startswith("_") for part in module.split("."))
            or not _is_a_dotted_identifier_path(qualname)
        ):
            return [f"{shown(module)}:{shown(qualname)}"]
        return []
    if kind == "list":
        return unimportable_models(ref["item"])
    if kind == "dict":
        return unimportable_models(ref["value"])
    if kind == "optional":
        return unimportable_models(ref["inner"])
    if kind == "stream":
        return unimportable_models(ref["item"])
    return []

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

import enum
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
    PydanticUserError,
    TypeAdapter,
    ValidationError,
    create_model,
)

SCALARS: dict[type, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    type(None): "none",
}
_SCALARS_BY_NAME = {name: tp for tp, name in SCALARS.items()}

_RESERVED_PARAM_NAMES = set(dir(BaseModel)) | {
    "__config__",
    "__base__",
    "__module__",
    "__validators__",
    "__cls_kwargs__",
    "model_config",
}

_RESERVED_RPC_METHOD_NAMES = {
    "verify",
    "close",
    "service",
    "namespace",
    "timeout",
    "headers",
    "SERVICE",
    "VERSION",
    "DESCRIPTION_HASH",
    "SIGNATURES",
}

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


def type_ref(tp: Any) -> dict[str, Any]:
    """Annotation -> TypeRef. Raises UnsupportedType for anything else."""
    if get_origin(tp) is Annotated:
        base = type_ref(get_args(tp)[0])
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
        schema = tp.model_json_schema()
        schema_hash = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]
        return {
            "kind": "model",
            "module": tp.__module__,
            "qualname": tp.__qualname__,
            "schema_hash": schema_hash,
        }
    is_opt, inner = _is_optional(tp)
    if is_opt:
        return {"kind": "optional", "inner": type_ref(inner)}
    origin = get_origin(tp)
    if origin is list:
        (item,) = get_args(tp)
        return {"kind": "list", "item": type_ref(item)}
    if origin is dict:
        key, value = get_args(tp)
        if key is not str:
            raise UnsupportedType(f"dict keys must be str, got {_name(key)}; this is unsupported")
        return {"kind": "dict", "value": type_ref(value)}
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


def python_type(ref: dict[str, Any]) -> Any:
    """TypeRef -> annotation. Imports models by module and qualname."""
    kind = ref["kind"]
    if kind == "scalar":
        return _SCALARS_BY_NAME[ref["name"]]
    if kind == "model":
        import importlib

        obj: Any = importlib.import_module(ref["module"])
        for part in ref["qualname"].split("."):
            obj = getattr(obj, part)
        return obj
    if kind == "list":
        return list[python_type(ref["item"])]  # type: ignore[misc]
    if kind == "dict":
        return dict[str, python_type(ref["value"])]  # type: ignore[misc]
    if kind == "optional":
        return python_type(ref["inner"]) | None
    if kind == "literal":
        return Literal[tuple(ref["values"])]
    raise UnsupportedType(f"unknown TypeRef kind {kind!r}")


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
    return_adapter: TypeAdapter
    takes_correlation_id: bool
    payload_model: type[BaseModel]
    doc: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    doc_summary: str | None = None
    doc_description: str | None = None

    @property
    def description(self) -> str | None:
        return self.doc_description


def collect_model_schemas(tp: Any, out: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recursively collect Pydantic models into out keyed by schema_hash."""
    if out is None:
        out = {}
    if tp is None:
        return out
    if isinstance(tp, HandlerSpec):
        for p in tp.params:
            collect_model_schemas(p.annotation, out)
        collect_model_schemas(tp.return_annotation, out)
        return out
    if isinstance(tp, ParamSpec):
        return collect_model_schemas(tp.annotation, out)

    origin = get_origin(tp)
    if origin is Annotated:
        return collect_model_schemas(get_args(tp)[0], out)
    if inspect.isclass(tp) and issubclass(tp, BaseModel):
        schema = tp.model_json_schema()
        schema_hash = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]
        if schema_hash not in out:
            out[schema_hash] = schema
            for field_info in tp.model_fields.values():
                collect_model_schemas(field_info.annotation, out)
        return out
    if origin in (list, set, frozenset, tuple, dict):
        for arg in get_args(tp):
            collect_model_schemas(arg, out)
        return out
    if origin is Union or origin is types.UnionType:
        for arg in get_args(tp):
            collect_model_schemas(arg, out)
        return out
    return out


class UntypedHandler(TypeError):
    """An @rpc handler that is not fully annotated. The service refuses to start."""


def build_handler_spec(name: str, func: Callable, *, owner: type) -> HandlerSpec:
    """Read one handler's contract from its signature, or refuse by name.

    Every refusal names `Owner.handler` and the offending parameter or the
    return, because this fires at service start and the operator reading it
    has a whole class of handlers to choose between.
    """
    qual = f"{owner.__qualname__}.{name}"
    if name in _RESERVED_RPC_METHOD_NAMES:
        raise UntypedHandler(
            f"{qual}: RPC handler name {name!r} conflicts with ServiceClient member; choose a different name"
        )
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
        if pname not in hints:
            raise UntypedHandler(f"{qual}: parameter {pname!r} has no annotation")
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
        return_ref = type_ref(hints["return"])
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
        return_adapter=TypeAdapter(hints["return"]),
        takes_correlation_id=takes_cid,
        payload_model=payload_model,
        doc=doc_summary,
        doc_summary=doc_summary,
        doc_description=doc_description,
    )


def unimportable_models(ref: dict[str, Any]) -> list[str]:
    """``"module:qualname"`` for every model in a TypeRef a client cannot import.

    Refuse models whose module is private or
    `__main__`, because the generated file imports it by module and qualname.
    A service with such a model runs perfectly well; only a client generated
    from it would not import.

    One compact identifier per model, not a sentence, because the command joins
    them into a single line and adds the remedy once.
    """
    kind = ref["kind"]
    if kind == "model":
        module = ref["module"]
        qualname = ref["qualname"]
        if (
            module == "__main__"
            or any(part.startswith("_") for part in module.split("."))
            or not all(
                part.isidentifier() and not keyword.iskeyword(part) for part in qualname.split(".")
            )
        ):
            return [f"{module}:{qualname}"]
        return []
    if kind == "list":
        return unimportable_models(ref["item"])
    if kind == "dict":
        return unimportable_models(ref["value"])
    if kind == "optional":
        return unimportable_models(ref["inner"])
    return []

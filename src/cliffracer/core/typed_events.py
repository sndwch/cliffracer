"""Typed domain events: listener annotations define incoming event schemas.

Inspects method signatures decorated with @listener or @broadcast, builds
structural specifications, synthesizes Pydantic payload models with extra="forbid",
and enforces startup validation identical to RPC.
"""

from __future__ import annotations

import functools
import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    PydanticUserError,
    TypeAdapter,
    ValidationError,
    create_model,
)

from .typed_rpc import (
    _RESERVED_PARAM_NAMES,
    ParamSpec,
    UnsupportedType,
    UntypedHandler,
    _name,
    _valid_cid_annotation,
    collect_model_schemas,
    refuse_a_parameter_alias,
    require_finite_json,
    type_ref,
)


@dataclass(frozen=True)
class EventHandlerSpec:
    """Specification and synthesized validation model for one event listener or broadcast handler."""

    name: str
    params: list[ParamSpec]
    payload_model: type[BaseModel]
    takes_subject: bool
    takes_correlation_id: bool
    is_single_model_param: bool
    single_model_param_name: str | None
    doc: str | None = None
    doc_summary: str | None = None
    doc_description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def build_event_spec(name: str, func: Callable[..., Any], *, owner: type) -> EventHandlerSpec:
    """Read an event handler's contract from its signature, or refuse with UntypedHandler.

    Invariants:
    - Position 0 'self' skipped if not staticmethod.
    - Rejects positional-only parameters.
    - Rejects *args (VAR_POSITIONAL) and **kwargs (VAR_KEYWORD).
    - Requires explicit type hints for all parameters (including subject and correlation_id).
    - If subject is declared, must be annotated as str.
    - If correlation_id is declared, must be str or str | None.
    - Synthesizes Pydantic model with extra='forbid' or binds explicit BaseModel parameter.
    """
    qual = f"{owner.__qualname__}.{name}"

    try:
        hints = typing.get_type_hints(func, include_extras=True)
    except Exception as exc:
        raise UntypedHandler(f"{qual}: type hints do not resolve: {exc}") from exc

    sig = inspect.signature(func)
    domain_params: list[ParamSpec] = []
    takes_subject = False
    takes_cid = False

    is_static = isinstance(func, staticmethod) or isinstance(
        inspect.getattr_static(owner, name, None), staticmethod
    )

    for idx, (pname, p) in enumerate(sig.parameters.items()):
        if idx == 0 and pname == "self" and not is_static:
            continue

        if p.kind is p.POSITIONAL_ONLY:
            raise UntypedHandler(
                f"{qual}: positional-only parameter {pname!r} is not allowed on an event handler"
            )

        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise UntypedHandler(
                f"{qual}: *{pname} is not allowed on an event handler; all parameters must be explicitly annotated"
            )

        if pname == "subject":
            takes_subject = True
            if pname not in hints or hints[pname] is not str:
                hint_str = _name(hints[pname]) if pname in hints else "unannotated"
                raise UntypedHandler(
                    f"{qual}: parameter 'subject' must be annotated as str, got {hint_str}"
                )
            continue

        if pname == "correlation_id":
            takes_cid = True
            if pname not in hints or not _valid_cid_annotation(hints[pname]):
                hint_str = _name(hints[pname]) if pname in hints else "unannotated"
                raise UntypedHandler(
                    f"{qual}: parameter 'correlation_id' annotation must be str or str | None, got {hint_str}"
                )
            continue

        if pname.startswith("_"):
            raise UntypedHandler(
                f"{qual}: parameter {pname!r} starting with '_' cannot be a valid event parameter"
            )

        if pname in _RESERVED_PARAM_NAMES:
            raise UntypedHandler(
                f"{qual}: parameter {pname!r} conflicts with BaseModel member; choose a different name"
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

        domain_params.append(
            ParamSpec(
                name=pname,
                annotation=hints[pname],
                ref=ref,
                adapter=adapter,
                has_default=has_default,
                default=p.default if has_default else None,
            )
        )

    # Payload model synthesis or binding
    is_single_model_param = False
    single_model_param_name = None

    explicit_model: type[BaseModel] | None = None
    if len(domain_params) == 1:
        ann = domain_params[0].annotation
        actual_type = get_args(ann)[0] if get_origin(ann) is Annotated else ann
        if inspect.isclass(actual_type) and issubclass(actual_type, BaseModel):
            explicit_model = actual_type

    if explicit_model is not None:
        is_single_model_param = True
        single_model_param_name = domain_params[0].name
        payload_model = explicit_model
    else:
        fields = {
            p.name: (p.annotation, p.default if p.has_default else ...) for p in domain_params
        }
        try:
            payload_model = create_model(  # type: ignore[call-overload]
                f"{owner.__name__}_{name}_EventPayload",
                __config__=ConfigDict(extra="forbid", validate_default=True),
                **fields,
            )
        except (ValidationError, PydanticUserError) as exc:
            raise UntypedHandler(f"{qual}: payload model creation failed: {exc}") from exc

    raw_doc = inspect.getdoc(func)
    doc_summary = None
    if raw_doc:
        lines = [line.strip() for line in raw_doc.splitlines()]
        doc_summary = next((line for line in lines if line), None)

    return EventHandlerSpec(
        name=name,
        params=domain_params,
        payload_model=payload_model,
        takes_subject=takes_subject,
        takes_correlation_id=takes_cid,
        is_single_model_param=is_single_model_param,
        single_model_param_name=single_model_param_name,
        doc=doc_summary,
        doc_summary=doc_summary,
        doc_description=raw_doc,
    )


def build_registered_event_spec(
    handler: Callable[..., Any], *, owner: type
) -> EventHandlerSpec | None:
    """Read the contract of a handler added at runtime, or None when it cannot be read.

    Discovery refuses a handler it cannot read with UntypedHandler. Registering one at runtime
    has always been accepted, and a handler with no spec is called with the payload as keywords,
    so the refusal is not raised here. `owner` names the generated payload model.
    """
    name = getattr(handler, "__name__", None) or type(handler).__name__
    try:
        return build_event_spec(name, handler, owner=owner)
    except (TypeError, ValueError):
        return None


def build_validated_event_spec(
    name: str,
    func: Callable[..., Any],
    *,
    owner: type,
    schema: type[BaseModel],
) -> EventHandlerSpec:
    """Validate the invocation contract of a schema-validated listener.

    The event dispatcher passes the declared schema instance to the handler's
    sole payload parameter. Subject and correlation metadata are optional
    keyword parameters; no other payload parameters can be supplied on this
    path.
    """
    qual = f"{owner.__qualname__}.{name}"
    schema_name = getattr(schema, "__name__", repr(schema))

    try:
        hints = typing.get_type_hints(func, include_extras=True)
    except Exception as exc:
        raise UntypedHandler(f"{qual}: type hints do not resolve: {exc}") from exc

    sig = inspect.signature(func)
    is_static = isinstance(func, staticmethod) or isinstance(
        inspect.getattr_static(owner, name, None), staticmethod
    )
    payload: tuple[str, inspect.Parameter] | None = None
    takes_subject = False
    takes_cid = False

    for idx, (pname, parameter) in enumerate(sig.parameters.items()):
        if idx == 0 and pname == "self" and not is_static:
            continue
        if parameter.kind is parameter.POSITIONAL_ONLY:
            raise UntypedHandler(
                f"{qual}: positional-only parameter {pname!r} is not allowed on an event handler"
            )
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise UntypedHandler(
                f"{qual}: *{pname} is not allowed on an event handler; all parameters "
                "must be explicitly annotated"
            )
        if pname == "subject":
            takes_subject = True
            if pname not in hints or hints[pname] is not str:
                hint_str = _name(hints[pname]) if pname in hints else "unannotated"
                raise UntypedHandler(
                    f"{qual}: parameter 'subject' must be annotated as str, got {hint_str}"
                )
            continue
        if pname == "correlation_id":
            takes_cid = True
            if pname not in hints or not _valid_cid_annotation(hints[pname]):
                hint_str = _name(hints[pname]) if pname in hints else "unannotated"
                raise UntypedHandler(
                    f"{qual}: parameter 'correlation_id' annotation must be str or "
                    f"str | None, got {hint_str}"
                )
            continue
        if pname.startswith("_"):
            raise UntypedHandler(
                f"{qual}: parameter {pname!r} starting with '_' cannot be a valid event parameter"
            )
        if pname in _RESERVED_PARAM_NAMES:
            raise UntypedHandler(
                f"{qual}: parameter {pname!r} conflicts with BaseModel member; "
                "choose a different name"
            )
        if pname not in hints:
            raise UntypedHandler(f"{qual}: parameter {pname!r} has no annotation")
        if payload is not None:
            raise UntypedHandler(
                f"{qual}: @validated_listener must declare exactly one payload parameter "
                f"annotated as {schema_name}; optional 'subject: str' and "
                "'correlation_id: str | None' parameters are also accepted"
            )

        annotation = hints[pname]
        actual_type = get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation
        if not inspect.isclass(actual_type) or not issubclass(actual_type, BaseModel):
            raise UntypedHandler(
                f"{qual}: @validated_listener payload parameter {pname!r} must be "
                f"annotated with a Pydantic model for the declared {schema_name} schema"
            )
        if not issubclass(schema, actual_type):
            raise UntypedHandler(
                f"{qual}: declared {schema_name} schema is not compatible with payload "
                f"parameter {pname!r} annotated as {_name(actual_type)}; use the declared "
                "schema, a shared Pydantic base model, or BaseModel"
            )
        payload = (pname, parameter)

    if payload is None:
        raise UntypedHandler(
            f"{qual}: @validated_listener must declare exactly one payload parameter "
            f"annotated as {schema_name}; optional 'subject: str' and "
            "'correlation_id: str | None' parameters are also accepted"
        )

    payload_name, parameter = payload
    has_default = parameter.default is not inspect.Parameter.empty
    raw_doc = inspect.getdoc(func)
    doc_summary = None
    if raw_doc:
        lines = [line.strip() for line in raw_doc.splitlines()]
        doc_summary = next((line for line in lines if line), None)

    require_finite_json(qual, "the payload model", functools.partial(collect_model_schemas, schema))
    param = ParamSpec(
        name=payload_name,
        annotation=schema,
        ref=type_ref(schema),
        adapter=TypeAdapter(schema),
        has_default=has_default,
        default=parameter.default if has_default else None,
    )
    return EventHandlerSpec(
        name=name,
        params=[param],
        payload_model=schema,
        takes_subject=takes_subject,
        takes_correlation_id=takes_cid,
        is_single_model_param=True,
        single_model_param_name=payload_name,
        doc=doc_summary,
        doc_summary=doc_summary,
        doc_description=raw_doc,
    )

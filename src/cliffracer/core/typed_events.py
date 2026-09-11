"""Typed domain events: listener annotations define incoming event schemas.

Inspects method signatures decorated with @listener or @broadcast, builds
structural specifications, synthesizes Pydantic payload models with extra="forbid",
and enforces startup validation identical to RPC.
"""

from __future__ import annotations

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

    if len(domain_params) == 1:
        ann = domain_params[0].annotation
        actual_type = get_args(ann)[0] if get_origin(ann) is Annotated else ann
        if inspect.isclass(actual_type) and issubclass(actual_type, BaseModel):
            is_single_model_param = True
            single_model_param_name = domain_params[0].name
            payload_model = actual_type
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

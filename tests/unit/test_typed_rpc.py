"""Tests verifying Annotation -> TypeRef codec and supported type rules."""

import enum
import hashlib
import json
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field, ValidationError

from cliffracer.core.typed_rpc import (
    SCALARS,
    UnsupportedType,
    UntypedHandler,
    build_handler_spec,
    python_type,
    type_ref,
    unimportable_models,
)

pytestmark = pytest.mark.unit


class Order(BaseModel):
    sku: str
    qty: int


ORDER_SCHEMA_HASH = hashlib.sha256(
    json.dumps(Order.model_json_schema(), sort_keys=True).encode()
).hexdigest()[:16]
ORDER_REF = {
    "kind": "model",
    "module": __name__,
    "qualname": "Order",
    "schema_hash": ORDER_SCHEMA_HASH,
}


@pytest.mark.parametrize(
    ("tp", "ref"),
    [
        (str, {"kind": "scalar", "name": "str"}),
        (int, {"kind": "scalar", "name": "int"}),
        (float, {"kind": "scalar", "name": "float"}),
        (bool, {"kind": "scalar", "name": "bool"}),
        (type(None), {"kind": "scalar", "name": "none"}),
        (Order, ORDER_REF),
        (list[int], {"kind": "list", "item": {"kind": "scalar", "name": "int"}}),
        (
            dict[str, Order],
            {
                "kind": "dict",
                "value": ORDER_REF,
            },
        ),
        (int | None, {"kind": "optional", "inner": {"kind": "scalar", "name": "int"}}),
        (Literal["a", "b"], {"kind": "literal", "values": ["a", "b"]}),
        (
            list[Order | None],
            {
                "kind": "list",
                "item": {
                    "kind": "optional",
                    "inner": ORDER_REF,
                },
            },
        ),
    ],
)
def test_type_ref_round_trips(tp, ref):
    assert type_ref(tp) == ref
    assert type_ref(python_type(ref)) == ref


@pytest.mark.parametrize("tp", [object, list, dict, dict[int, str], set[int], bytes, complex])
def test_unsupported_types_are_refused_by_name(tp):
    with pytest.raises(UnsupportedType) as e:
        type_ref(tp)
    assert "unsupported" in str(e.value)


def test_any_is_refused():
    from typing import Any

    with pytest.raises(UnsupportedType):
        type_ref(Any)


def test_a_private_model_module_is_named_by_the_generator_check_not_by_type_ref():
    """Verify private model modules are accepted by type_ref."""

    class Hidden(BaseModel):
        x: int

    # __qualname__ too, not only __module__: a class defined inside a test
    # function carries `test_....<locals>.Hidden`, and the identifier the
    # generator prints is the one a client would import.
    Hidden.__module__ = "_private"
    Hidden.__qualname__ = "Hidden"
    ref = type_ref(Hidden)
    assert ref["kind"] == "model"
    assert ref["module"] == "_private"
    assert ref["qualname"].endswith("Hidden")

    assert unimportable_models(ref) == ["_private:Hidden"]
    assert unimportable_models({"kind": "list", "item": ref}) == ["_private:Hidden"]


def test_unimportable_models_looks_inside_containers():
    class Hidden(BaseModel):
        x: int

    Hidden.__module__ = "__main__"
    Hidden.__qualname__ = "Hidden"
    expected = ["__main__:Hidden"]
    assert unimportable_models(type_ref(list[Hidden])) == expected
    assert unimportable_models(type_ref(dict[str, Hidden])) == expected
    assert unimportable_models(type_ref(Hidden | None)) == expected


def test_CONTROL_an_importable_model_has_no_offenders():
    assert unimportable_models(type_ref(Order)) == []
    assert unimportable_models(type_ref(list[Order])) == []


def test_CONTROL_scalars_table_is_the_five_json_scalars():
    assert set(SCALARS.values()) == {"str", "int", "float", "bool", "none"}


class _Svc:
    async def create(self, order: Order, note: str = "") -> Order:
        return order

    async def with_cid(self, sku: str, correlation_id: str | None = None) -> str:
        return sku

    async def no_return(self, sku: str):
        return sku

    async def no_param(self, sku) -> str:
        return sku

    async def bad_type(self, sku: bytes) -> str:
        return "x"


def test_spec_reads_params_defaults_and_return():
    spec = build_handler_spec("create", _Svc.create, owner=_Svc)
    assert [p.name for p in spec.params] == ["order", "note"]
    assert spec.params[0].ref["kind"] == "model"
    assert spec.params[1].has_default and spec.params[1].default == ""
    assert spec.return_ref == ORDER_REF
    assert spec.takes_correlation_id is False


def test_correlation_id_is_excluded_from_params_and_flagged():
    spec = build_handler_spec("with_cid", _Svc.with_cid, owner=_Svc)
    assert [p.name for p in spec.params] == ["sku"]
    assert spec.takes_correlation_id is True


@pytest.mark.parametrize(
    ("method", "needle"),
    [("no_return", "return"), ("no_param", "sku"), ("bad_type", "bytes")],
)
def test_untyped_or_unsupported_handler_refuses_by_name(method, needle):
    with pytest.raises(UntypedHandler) as e:
        build_handler_spec(method, getattr(_Svc, method), owner=_Svc)
    msg = str(e.value)
    assert f"_Svc.{method}" in msg and needle in msg


def test_CONTROL_the_annotated_form_of_each_refusal_passes():
    class Ok:
        async def no_return(self, sku: str) -> str: ...
        async def no_param(self, sku: str) -> str: ...
        async def bad_type(self, sku: str) -> str: ...

    for m in ("no_return", "no_param", "bad_type"):
        build_handler_spec(m, getattr(Ok, m), owner=Ok)


def test_model_schema_hash_changes_with_field_modifications():
    """Verify model schema hash changes when model fields are modified."""

    class ModelA(BaseModel):
        sku: str
        qty: int

    class ModelB(BaseModel):
        sku: str
        qty: str
        extra: float = 0.0

    ref_a = type_ref(ModelA)
    ref_b = type_ref(ModelB)
    assert "schema_hash" in ref_a
    assert "schema_hash" in ref_b
    assert ref_a["schema_hash"] != ref_b["schema_hash"]


def test_annotated_constraints_preserved_in_payload_model():
    """Verify Annotated/Field constraints are preserved in TypeRef and enforced in payload."""

    class Svc:
        async def constrained(self, age: Annotated[int, Field(ge=18)]) -> int:
            return age

    spec = build_handler_spec("constrained", Svc.constrained, owner=Svc)
    assert spec.params[0].ref == {
        "kind": "scalar",
        "name": "int",
        "constraints": {"ge": 18},
    }
    with pytest.raises(ValidationError):
        spec.payload_model.model_validate({"age": 16})
    validated = spec.payload_model.model_validate({"age": 21})
    assert validated.age == 21


def test_positional_only_parameter_rejected():
    """Verify positional-only parameter (/) is rejected with UntypedHandler."""

    class Svc:
        async def posonly(self, a: str, /, b: str = "b") -> str:
            return a + b

    with pytest.raises(UntypedHandler) as exc:
        build_handler_spec("posonly", Svc.posonly, owner=Svc)
    msg = str(exc.value)
    assert "positional-only parameter 'a' is not allowed on an RPC handler" in msg


def test_invalid_default_value_rejected():
    """Verify default value not matching annotation is rejected with UntypedHandler."""

    class Svc:
        async def wrong_default(self, x: int = None) -> int:  # type: ignore[assignment]
            return x

    with pytest.raises(UntypedHandler) as exc:
        build_handler_spec("wrong_default", Svc.wrong_default, owner=Svc)
    msg = str(exc.value)
    assert "default value None for parameter 'x' does not match annotation" in msg


@pytest.mark.parametrize(
    "bad_name", ["model_config", "model_dump", "model_dump_json", "copy", "dict"]
)
def test_reserved_basemodel_parameter_name_rejected(bad_name):
    """Verify parameter names colliding with BaseModel internals are rejected at startup."""

    class Svc:
        pass

    code = f"async def f(self, {bad_name}: str) -> str: return {bad_name}"
    ns = {}
    exec(code, ns)
    Svc.bad_method = ns["f"]

    with pytest.raises(UntypedHandler) as exc:
        build_handler_spec("bad_method", Svc.bad_method, owner=Svc)
    assert f"parameter '{bad_name}' conflicts with BaseModel member" in str(exc.value)


def test_staticmethod_with_self_parameter_is_not_skipped():
    """Verify staticmethod self parameter is treated as real parameter, not receiver."""

    class Svc:
        @staticmethod
        async def stat(self: int, b: str) -> str:
            return b

    spec = build_handler_spec("stat", Svc.stat, owner=Svc)
    assert [p.name for p in spec.params] == ["self", "b"]
    assert spec.params[0].ref == {"kind": "scalar", "name": "int"}


def test_correlation_id_annotation_validated():
    """Verify correlation_id annotation must be str or str | None if provided."""

    class Svc:
        async def bad_cid(self, correlation_id: int) -> str:
            return "ok"

        async def ok_cid(self, correlation_id: str | None = None) -> str:
            return "ok"

    with pytest.raises(UntypedHandler) as exc:
        build_handler_spec("bad_cid", Svc.bad_cid, owner=Svc)
    assert "correlation_id annotation must be str or str | None, got int" in str(exc.value)

    spec = build_handler_spec("ok_cid", Svc.ok_cid, owner=Svc)
    assert spec.takes_correlation_id is True
    assert len(spec.params) == 0


class _NumEnum(int, enum.Enum):
    ONE = 1
    TWO = 2


class _StrEnum(str, enum.Enum):
    ALPHA = "alpha"
    BETA = "beta"


class _GenericPage[T](BaseModel):
    items: list[T]


def test_unimportable_models_rejects_non_identifier_qualnames():
    # Parametrized generic Page[int]
    page_ref = type_ref(_GenericPage[int])
    assert unimportable_models(page_ref) == [f"{_GenericPage.__module__}:_GenericPage[int]"]
    assert unimportable_models({"kind": "list", "item": page_ref}) == [
        f"{_GenericPage.__module__}:_GenericPage[int]"
    ]

    # Local model inside function
    def _make_local():
        class LocalModel(BaseModel):
            val: int

        return LocalModel

    local_cls = _make_local()
    local_ref = type_ref(local_cls)
    unimp = unimportable_models(local_ref)
    assert len(unimp) == 1
    assert "<locals>" in unimp[0]


def test_literal_over_enum_normalizes_to_scalar():
    # Enum member values are unwrapped to plain int/str scalars
    int_enum_ref = type_ref(Literal[_NumEnum.ONE, _NumEnum.TWO])
    assert int_enum_ref == {"kind": "literal", "values": [1, 2]}

    str_enum_ref = type_ref(Literal[_StrEnum.ALPHA, _StrEnum.BETA])
    assert str_enum_ref == {"kind": "literal", "values": ["alpha", "beta"]}


def test_empty_literal_is_refused():
    # Empty literal refused with UnsupportedType
    with pytest.raises(UnsupportedType) as exc_info:
        type_ref(Literal[()])
    assert "unsupported" in str(exc_info.value)
    assert "Literal cannot be empty" in str(exc_info.value)


def test_positional_only_correlation_id_rejected():
    """Verify positional-only correlation_id is rejected with UntypedHandler."""

    class Svc:
        async def posonly_cid(self, correlation_id: str | None = None, /) -> str:
            return "ok"

    with pytest.raises(UntypedHandler) as exc:
        build_handler_spec("posonly_cid", Svc.posonly_cid, owner=Svc)
    msg = str(exc.value)
    assert "positional-only parameter 'correlation_id' is not allowed on an RPC handler" in msg


def test_handler_named_after_service_client_member_rejected():
    """Verify handlers named after ServiceClient members are rejected at startup."""

    class Svc:
        async def verify(self, token: str) -> bool:
            return True

        async def close(self) -> None:
            pass

    with pytest.raises(UntypedHandler) as exc:
        build_handler_spec("verify", Svc.verify, owner=Svc)
    assert "conflicts with ServiceClient member" in str(exc.value)

    with pytest.raises(UntypedHandler) as exc:
        build_handler_spec("close", Svc.close, owner=Svc)
    assert "conflicts with ServiceClient member" in str(exc.value)


def test_handler_parameter_starting_with_underscore_rejected():
    """Verify parameters starting with underscore are rejected at startup."""

    class Svc:
        async def query(self, _count: int) -> int:
            return _count

    with pytest.raises(UntypedHandler) as exc:
        build_handler_spec("query", Svc.query, owner=Svc)
    assert "starting with '_' cannot be a valid RPC parameter" in str(exc.value)


def test_handlerspec_preserves_multiline_docstring_fields():
    """Verify build_handler_spec populates doc, doc_summary, and doc_description."""

    class Svc:
        async def multi(self, x: int) -> int:
            """First line summary.

            Second paragraph with extra details.
            """
            return x

    spec = build_handler_spec("multi", Svc.multi, owner=Svc)
    assert spec.doc == "First line summary."
    assert spec.doc_summary == "First line summary."
    assert spec.doc_description is not None
    assert "Second paragraph with extra details." in spec.doc_description
    assert spec.description == spec.doc_description


def test_collect_model_schemas_from_spec_and_nested_models():
    """Verify collect_model_schemas extracts all schemas from HandlerSpec."""
    from cliffracer.core.typed_rpc import collect_model_schemas

    class Item(BaseModel):
        sku: str

    class Container(BaseModel):
        items: list[Item]

    class Svc:
        async def handle(self, payload: Container) -> Item:
            """Handler doc."""
            return payload.items[0]

    spec = build_handler_spec("handle", Svc.handle, owner=Svc)
    schemas = collect_model_schemas(spec)
    titles = {s.get("title") for s in schemas.values() if isinstance(s, dict)}
    assert "Container" in titles
    assert "Item" in titles

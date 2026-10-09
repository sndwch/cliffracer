"""A service says, per default that holds a model, whether a client can rebuild it from the dump.

The description carries a default as its JSON-mode dump. A generated client builds the models in it
(`Item.model_validate(dump, strict=False)`) only when the service has said it can: `rebuildable`, a
key of the parameter, decided where the real model classes are. It is true only when validating the
dump leniently gives back a value equal to the service's own, which dumps to the same JSON values.
Everything the dump can hide is then visible to the check and needs no guess from a schema: aliases
that swap, a model that keeps extra keys, a union whose JSON form fits more than one member, a
serializer that changes a type or is not idempotent, `Json[...]`, base64 bytes, a masked secret.

The decision does not depend on key order, so it holds over both ways a generator gets a description:
the class in process, and the bytes of `{service}.describe`, which are written with sorted keys.
"""

import asyncio
import inspect
import json
import sys
import warnings
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    TypeAdapter,
    field_serializer,
    model_serializer,
    model_validator,
)

from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.typed_rpc import type_ref
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import Description, _is_rebuildable, canonical, describe
from tests.fixtures.model_defaults import (
    Aliasing,
    Awkward,
    Shapes,
    Stock,
    Strictness,
    Swapped,
    Unions,
)

pytestmark = pytest.mark.unit

SERVICES = [Stock, Aliasing, Strictness, Unions, Shapes, Awkward]

# What each default's own model says. True: validating the dump leniently gives the service's value.
EXPECTED: dict[str, bool | None] = {
    "Stock.boxed.box": True,
    "Stock.by_name.items": True,
    "Stock.many.items": True,
    "Stock.maybe.item": True,  # an optional model that is None
    "Stock.maybe.other": True,
    "Stock.one.item": True,
    "Aliasing.by_name.items": True,  # populate_by_name: the field name is accepted
    "Aliasing.many.items": False,  # an alias and no populate_by_name
    "Aliasing.nested.wrapper": False,  # an alias-only model inside a model
    "Aliasing.plain.item": True,
    "Aliasing.populatable.item": True,
    "Aliasing.strict.item": False,
    "Strictness.annotated.value": True,  # strict by Strict(); lenient validation takes the dump
    "Strictness.by_name.values": True,
    "Strictness.field_level.value": True,
    "Strictness.many.values": True,
    "Strictness.model_level.value": True,
    "Strictness.money.value": True,  # a Decimal alone: its string is the Decimal
    "Unions.deep.value": True,  # unions that hold their member: int | str given "1" or 2
    "Unions.either.value": True,
    "Unions.listed.values": True,
    "Unions.midnight.value": False,  # date | datetime at midnight rebuilds as the date
    "Unions.plain.value": True,
    "Unions.tenth.value": False,  # float | Decimal("0.1") rebuilds as a float
    "Shapes.bare_extra.value": True,  # extra="allow" keeps what it is given, and was given nothing
    "Shapes.extra.value": True,  # and keeps the stray key it was given
    "Shapes.household.value": True,  # Cat | Dog, told apart by a Literal
    "Shapes.swapped.value": False,  # aliases that are each other's names swap their values
    "Awkward.adds_one.value": False,  # a before-validator that is not idempotent
    "Awkward.appends.value": False,  # a validator that is not idempotent
    "Awkward.base64_both.value": True,  # bytes dumped and validated as base64
    "Awkward.base64_out.value": False,  # bytes dumped as base64, validated as text
    "Awkward.changed.value": False,  # a field serializer that changes the type
    "Awkward.doubled.value": False,  # a serializer that is not idempotent
    "Awkward.jsoned.value": False,  # Json[...]: the dump is the parsed value, not the text
    "Awkward.secret.value": False,  # the dump is masked
    "Awkward.whole.value": False,  # a model serializer that writes another shape
}


def _flags(description: Description, service: type) -> dict[str, bool | None]:
    return {
        f"{service.__name__}.{method.name}.{param.name}": param.rebuildable
        for method in description.methods
        for param in method.params
        if param.has_default
    }


def _described(service: type) -> Description:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return describe(service, service=service.__name__.lower(), version="1")


def _as_the_wire_has_it(description: Description) -> Description:
    """What a generator reads from `{service}.describe`: the handler's bytes, decoded."""
    return Description.from_dict(json.loads(canonical(description.to_dict())))


def test_each_default_is_flagged_as_its_own_model_decides():
    got: dict[str, bool | None] = {}
    for service in SERVICES:
        got |= _flags(_described(service), service)

    assert got == EXPECTED


def test_the_flag_is_on_the_parameter_only_when_its_type_holds_a_model():
    described = _described(Stock)
    strings = Description.from_dict(
        {
            "service": "s",
            "version": "1",
            "description_hash": "sha256:d",
            "methods": [
                {
                    "name": "m",
                    "doc": None,
                    "signature_hash": "sha256:m",
                    "returns": {"kind": "scalar", "name": "str"},
                    "params": [
                        {"name": "x", "type": {"kind": "scalar", "name": "int"}, "default": 1}
                    ],
                }
            ],
        }
    )

    assert all("rebuildable" in p.to_dict() for m in described.methods for p in m.params)
    assert "rebuildable" not in strings.methods[0].params[0].to_dict()


def test_a_parameter_with_no_model_has_the_description_it_always_had():
    class Plain(BaseModel):
        pass

    from cliffracer import CliffracerService, rpc

    class Service(CliffracerService):
        @rpc
        async def m(self, n: int = 1, tags: list[str] = [], q: Plain | None = None) -> int:  # noqa: B006
            return n

    params = _described(Service).methods[0].params

    assert [p.to_dict() for p in params] == [
        {"name": "n", "type": {"kind": "scalar", "name": "int"}, "default": 1},
        {
            "name": "tags",
            "type": {"kind": "list", "item": {"kind": "scalar", "name": "str"}},
            "default": [],
        },
        {
            "name": "q",
            "type": params[2].type,
            "default": None,
            "rebuildable": True,
        },
    ]


@pytest.mark.parametrize("said", [True, False, None, 1, 0, "true", "yes", [], {"x": 1}])
def test_a_description_is_read_as_rebuildable_only_by_a_boolean_it_carries(said):
    param = (
        Description.from_dict(
            {
                "service": "s",
                "version": "1",
                "description_hash": "sha256:d",
                "methods": [
                    {
                        "name": "m",
                        "doc": None,
                        "signature_hash": "sha256:m",
                        "returns": {"kind": "scalar", "name": "str"},
                        "params": [
                            {
                                "name": "x",
                                "type": {"kind": "scalar", "name": "int"},
                                "default": 1,
                                "rebuildable": said,
                            }
                        ],
                    }
                ],
            }
        )
        .methods[0]
        .params[0]
    )

    assert param.rebuildable == (said if isinstance(said, bool) else None)


# --- the criterion, clause by clause ---------------------------------------------------------------


class Always(BaseModel):
    """Equal to anything, so only the dumps can tell a rebuild from the original."""

    n: int

    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = None  # type: ignore[assignment]

    @field_serializer("n")
    def _bump(self, value: int) -> int:
        return value + 1


class Strictly(BaseModel):
    n: int


class Textual(BaseModel):
    """Dumps to a bare string, and its own validation would build it back from that string."""

    v: str

    @model_serializer
    def _as_text(self) -> str:
        return self.v

    @model_validator(mode="before")
    @classmethod
    def _from_text(cls, data: Any) -> Any:
        return {"v": data} if isinstance(data, str) else data


def _rebuildable(model: type[BaseModel], default: BaseModel, dump: Any) -> bool:
    return _is_rebuildable(model, type_ref(model), TypeAdapter(model), default, dump)


def test_a_dump_that_does_not_validate_is_not_rebuildable():
    assert _rebuildable(Strictly, Strictly(n=1), {"n": "not a number"}) is False


def test_a_rebuild_that_is_not_equal_to_the_services_value_is_not_rebuildable():
    assert _rebuildable(Strictly, Strictly(n=1), {"n": 2}) is False


def test_a_rebuild_that_dumps_to_other_json_values_is_not_rebuildable():
    dump = TypeAdapter(Always).dump_python(Always(n=1), mode="json")

    assert dump == {"n": 2}
    assert _rebuildable(Always, Always(n=1), dump) is False


def test_the_json_values_compared_are_the_values_and_not_their_order_or_spelling():
    assert _rebuildable(Strictly, Strictly(n=1), {"n": 1}) is True
    assert _rebuildable(Strictly, Strictly(n=1), {"n": 1.0}) is False  # 1.0 is another JSON value


def test_a_model_whose_dump_is_not_an_object_is_not_rebuildable():
    """A generated client builds a model only from an object, and writes any other dump as it is:
    the default would be the string, though the service's own model could read it."""
    assert TypeAdapter(Textual).dump_python(Textual(v="x"), mode="json") == "x"
    assert Textual.model_validate("x") == Textual(v="x")

    assert _rebuildable(Textual, Textual(v="x"), "x") is False


def test_CONTROL_a_model_that_rebuilds_exactly_is_rebuildable():
    dump = TypeAdapter(Strictly).dump_python(Strictly(n=1))

    assert _rebuildable(Strictly, Strictly(n=1), dump) is True


def test_a_default_is_rebuilt_as_the_client_builds_it_and_not_through_the_parameters_annotation():
    """An annotation can repair a model the client builds without it.

    `Swapped` rebuilds from its dump with its two values swapped. Behind a `BeforeValidator` that
    swaps the keys back, the parameter's own adapter gets the service's value, and the client, which
    runs `Swapped.model_validate` and not the annotation, does not.
    """

    def unswap(value: dict) -> dict:
        return {"a": value["b"], "b": value["a"]}

    annotation = Annotated[Swapped, BeforeValidator(unswap)]
    adapter = TypeAdapter(annotation)
    default = Swapped(b=1, a=2)
    dump = adapter.dump_python(default, mode="json")

    assert adapter.validate_python(dump, strict=False) == default  # the adapter alone is satisfied
    assert _is_rebuildable(annotation, type_ref(annotation), adapter, default, dump) is False


def test_CONTROL_a_constrained_list_of_models_is_rebuildable_when_the_client_builds_it_right():
    annotation = Annotated[list[Strictly], Field(min_length=1)]
    adapter = TypeAdapter(annotation)
    default = [Strictly(n=1)]
    dump = adapter.dump_python(default, mode="json")

    assert _is_rebuildable(annotation, type_ref(annotation), adapter, default, dump) is True


# --- both ways a generator gets the description -----------------------------------------------------


@pytest.mark.parametrize("service", SERVICES, ids=lambda service: service.__name__)
def test_the_description_over_the_wire_carries_the_same_flags_as_the_class(service):
    in_process = _described(service)
    on_the_wire = _as_the_wire_has_it(in_process)

    assert _flags(on_the_wire, service) == _flags(in_process, service)
    assert on_the_wire == in_process


def test_the_wire_orders_keys_differently_from_the_class():
    """So the pins below over the wire form are about a description the class does not give."""
    in_process = _described(Stock)
    on_the_wire = _as_the_wire_has_it(in_process)

    natural = list(in_process.methods[-1].params[0].default or {})
    sorted_ = list(on_the_wire.methods[-1].params[0].default or {})

    assert natural != sorted_
    assert sorted(natural) == sorted_
    assert emit(in_process) == emit(on_the_wire)


# --- every built default is the service's value and every payload is the dump -----------------------


def _import_client(source: str, tmp_path: Path, name: str):
    import importlib.util

    path = tmp_path / f"{name}_client.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(f"{name}_client_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _client_class(module):
    return next(
        value
        for value in vars(module).values()
        if inspect.isclass(value)
        and value.__name__.endswith("Client")
        and value.__module__ == module.__name__
    )


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")  # the dump of a dict default
@pytest.mark.parametrize("service", SERVICES, ids=lambda service: service.__name__)
@pytest.mark.parametrize("path", ["class", "wire"])
def test_every_built_default_is_the_services_value_and_every_payload_is_the_described_dump(
    service, path, tmp_path
):
    in_process = _described(service)
    description = in_process if path == "class" else _as_the_wire_has_it(in_process)
    module = _import_client(emit(description), tmp_path, service.__name__)
    client = _client_class(module)(nats_url="nats://broker.invalid:6999", verify=False)
    sent: dict[str, dict] = {}

    async def record(method, params, return_type):
        sent[method] = params
        return None

    client._call = record
    checked = built = refused = 0
    for method in description.methods:
        real = inspect.signature(getattr(service, method.name)).parameters
        generated = inspect.signature(getattr(type(client), method.name)).parameters
        defaults = [p for p in method.params if p.has_default]
        try:
            asyncio.run(getattr(client, method.name)())
        except RpcValidationError:
            # Refused before sending: the dict default fails the client's own check, as on main.
            refused += 1
            assert any(p.rebuildable is not True for p in defaults), (service, method.name)
            continue
        for param in defaults:
            checked += 1
            where = (service.__name__, method.name, param.name)
            default = generated[param.name].default
            if param.rebuildable is True and param.default is not None:
                built += 1
                assert default == real[param.name].default, where
            else:
                assert canonical(default) == canonical(param.default), where
            assert canonical(sent[method.name][param.name]) == canonical(param.default), where
    assert checked and built, service
    assert refused < len(description.methods), service

"""A handler whose contract holds `inf`, `-inf` or `nan` is refused by name, where it is declared.

The description is published as JSON and a parser in another language rejects a document that
holds one of those, so a single such default made the whole description unreadable to every
client written in anything else. `canonical` is strict about it, and the spec builders that both
the service at start and `describe` call refuse it, naming the handler and the parameter or the
model, so the two agree on the class of handler they refuse.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field, PlainSerializer

from cliffracer import CliffracerService, ConfigurationError, rpc, validated_listener
from cliffracer.core.typed_rpc import UntypedHandler
from cliffracer.introspect import canonical, describe

pytestmark = pytest.mark.unit

INF = float("inf")


def _refuse_to_serialise(value: int) -> int:
    raise ValueError("this value has no JSON form")


Unserialisable = Annotated[int, PlainSerializer(_refuse_to_serialise)]


class Capped(BaseModel):
    cap: float = INF


class Fine(BaseModel):
    cap: float | None = None


def _scalar_default():
    class S(CliffracerService):
        @rpc
        async def m(self, limit: float = INF) -> int: ...

    return S, "S.m", "the default of parameter 'limit'"


def _negative_infinity_default():
    class S(CliffracerService):
        @rpc
        async def m(self, limit: float = -INF) -> int: ...

    return S, "S.m", "the default of parameter 'limit'"


def _nan_default_in_a_list():
    class S(CliffracerService):
        @rpc
        async def m(self, limits: list[float] = [float("nan")]) -> int: ...  # noqa: B006

    return S, "S.m", "the default of parameter 'limits'"


def _a_bound_on_the_type():
    class S(CliffracerService):
        @rpc
        async def m(self, limit: Annotated[float, Field(ge=-INF)] = 1.0) -> int: ...

    return S, "S.m", "the type of parameter 'limit'"


def _a_bound_on_the_return():
    class S(CliffracerService):
        @rpc
        async def m(self) -> Annotated[float, Field(le=INF)]: ...

    return S, "S.m", "the return type"


def _a_model_parameter_with_a_non_finite_default():
    class S(CliffracerService):
        @rpc
        async def m(self, request: Capped) -> int: ...

    return S, "S.m", "a model in parameter 'request'"


def _a_model_the_return_carries():
    class S(CliffracerService):
        @rpc
        async def m(self) -> Capped: ...

    return S, "S.m", "a model in the return type"


def _a_validated_listener_model():
    class S(CliffracerService):
        @validated_listener("evt.a", Capped, fanout=True)
        async def on_a(self, event: Capped) -> None: ...

    return S, "S.on_a", "the payload model"


REFUSED = [
    _scalar_default,
    _negative_infinity_default,
    _nan_default_in_a_list,
    _a_bound_on_the_type,
    _a_bound_on_the_return,
    _a_model_parameter_with_a_non_finite_default,
    _a_model_the_return_carries,
    _a_validated_listener_model,
]


@pytest.mark.parametrize("build", REFUSED)
def test_describe_refuses_it_and_names_the_handler_and_the_part(build):
    cls, handler, part = build()

    with pytest.raises(UntypedHandler) as refused:
        describe(cls, service="s", version="1")

    message = str(refused.value)
    assert handler in message and part in message, message
    assert "float | None = None" in message, message


@pytest.mark.parametrize("build", REFUSED)
def test_the_service_refuses_the_same_thing_at_start_with_the_same_words(build):
    from cliffracer import ServiceConfig

    cls, _, _ = build()
    with pytest.raises(UntypedHandler) as described:
        describe(cls, service="s", version="1")

    with pytest.raises((UntypedHandler, ConfigurationError)) as started:
        cls(ServiceConfig(name="s", health_port=0))._discover_handlers()

    assert type(started.value) is UntypedHandler
    assert str(started.value) == str(described.value)


@pytest.mark.parametrize("value", [INF, -INF, float("nan")])
def test_canonical_does_not_write_a_value_json_cannot_carry(value):
    with pytest.raises(ValueError, match="not JSON compliant"):
        canonical({"default": value})


def _every_part_finite():
    class S(CliffracerService):
        @rpc
        async def m(
            self,
            limit: float | None = None,
            big: float = 1e308,
            zero: float = -0.0,
            bounded: Annotated[float, Field(ge=0, le=1e9)] = 1.0,
            model: Fine | None = None,
        ) -> Fine: ...

        @validated_listener("evt.a", Fine, fanout=True)
        async def on_a(self, event: Fine) -> None: ...

    return S


def _no_constant(name: str) -> Any:
    raise AssertionError(f"the description holds the non-JSON constant {name}")


def test_CONTROL_a_contract_of_finite_numbers_is_described_and_parses_as_strict_json():
    """The instrument: the refusal fires on non-finite numbers and not on the neighbours it
    could be mistaken for (a large finite float, negative zero, a bound, `None` for no limit)."""
    described = describe(_every_part_finite(), service="s", version="1")

    wire = canonical(described.to_dict())

    assert json.loads(wire, parse_constant=_no_constant)["methods"][0]["name"] == "m"


def test_a_default_that_cannot_be_serialised_is_describes_error_and_not_this_checks():
    """What the check leaves alone: it judges non-finite numbers and nothing else.

    A default pydantic cannot write as JSON fails when `describe` builds it, with pydantic's own
    error. The refusal here must not turn that into a claim about a number, and must not raise
    it from the spec builder where the service would then refuse to start for a default that was
    never the check's business.
    """
    from pydantic_core import PydanticSerializationError

    from cliffracer.core.typed_rpc import build_handler_spec

    class S(CliffracerService):
        @rpc
        async def m(self, thing: Unserialisable = 1) -> int: ...

    spec = build_handler_spec("m", S.m, owner=S)
    assert [p.name for p in spec.params] == ["thing"]

    with pytest.raises(PydanticSerializationError):
        describe(S, service="s", version="1")


def test_a_template_whose_settings_schema_holds_a_non_finite_number_is_refused_by_name():
    """`canonical` is also what pins a template's settings schema, so the same strictness
    reaches registration, where a bare `ValueError` would not say which template."""
    from dataclasses import replace

    from cliffracer.runners import TemplateCatalog
    from cliffracer.runners.contracts import TemplateError
    from tests.fixtures.shipment_templates import ShipmentSettings, shipment_template

    class Uncapped(ShipmentSettings):
        cap: float = INF

    template = replace(shipment_template(), settings_model=Uncapped)

    with pytest.raises(TemplateError) as refused:
        TemplateCatalog().register(template)

    message = str(refused.value)
    assert "'shipments'" in message and "'warehouse-a'" in message, message
    assert "float | None = None" in message, message


def test_CONTROL_a_template_with_a_finite_settings_schema_still_registers():
    from cliffracer.runners import TemplateCatalog
    from tests.fixtures.shipment_templates import shipment_template

    assert TemplateCatalog().register(shipment_template()).definition.name == "shipments"

"""A @validated_listener signature is refused by the rules a @listener's is, and a model binds as one.

`build_validated_event_spec` refuses each signature `build_event_spec` refuses: a positional-only
or variadic parameter, a `subject` that is not `str`, a `correlation_id` that is not `str` or
`str | None`, a name starting with `_` or naming a BaseModel member, and no payload parameter at
all. Its payload parameter must be a Pydantic model the declared schema is a subclass of, and an
`Annotated` model is the model. For a @listener, an `Annotated` model as the one parameter binds as
the model, and a default is validated into the payload, so a lax default arrives coerced.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, validated_listener
from cliffracer.core.typed_events import build_event_spec, build_validated_event_spec
from cliffracer.core.typed_rpc import UntypedHandler

pytestmark = pytest.mark.unit


class Order(BaseModel):
    n: int = 0


class Owner:
    pass


def _validated(func: Any) -> Any:
    return build_validated_event_spec(func.__name__, func, owner=Owner, schema=Order)


def positional_only(self, order: Order, /) -> None: ...
def var_positional(self, order: Order, *rest: Any) -> None: ...
def var_keyword(self, order: Order, **rest: Any) -> None: ...
def subject_not_str(self, order: Order, subject: int) -> None: ...
def correlation_id_not_str(self, order: Order, correlation_id: int) -> None: ...
def underscored(self, _order: Order) -> None: ...
def a_basemodel_member(self, model_dump: Order) -> None: ...
def no_payload(self) -> None: ...
def not_a_model(self, order: int) -> None: ...


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (positional_only, "positional-only parameter 'order' is not allowed"),
        (var_positional, r"\*rest is not allowed on an event handler"),
        (var_keyword, r"\*rest is not allowed on an event handler"),
        (subject_not_str, "parameter 'subject' must be annotated as str, got int"),
        (correlation_id_not_str, "parameter 'correlation_id' annotation must be str or"),
        (underscored, "parameter '_order' starting with '_'"),
        (a_basemodel_member, "parameter 'model_dump' conflicts with BaseModel member"),
        (no_payload, "must declare exactly one payload parameter annotated as Order"),
        (not_a_model, "payload parameter 'order' must be annotated with a Pydantic model"),
    ],
    ids=lambda v: v.__name__ if callable(v) else "",
)
def test_a_validated_listener_signature_a_listener_could_not_have_is_refused(handler, reason):
    with pytest.raises(UntypedHandler, match=reason):
        _validated(handler)


def test_discovery_refuses_a_validated_listener_with_no_payload_before_it_subscribes():
    class Desk(CliffracerService):
        @validated_listener("orders.placed", Order, fanout=True)
        async def placed(self) -> None: ...

    with pytest.raises(UntypedHandler, match="exactly one payload parameter"):
        Desk(ServiceConfig(name="desk", health_port=0))._discover_handlers()


def test_an_annotated_model_is_a_validated_listeners_payload():
    def annotated(self, order: Annotated[Order, "placed"]) -> None: ...

    assert _validated(annotated).single_model_param_name == "order"


def test_a_listener_whose_one_parameter_is_an_annotated_model_binds_the_model():
    def annotated(self, order: Annotated[Order, "placed"]) -> None: ...

    spec = build_event_spec("annotated", annotated, owner=Owner)

    assert (spec.is_single_model_param, spec.payload_model) == (True, Order)


def test_a_listener_default_is_validated_into_the_payload():
    def lax(self, n: int = "5") -> None: ...  # type: ignore[assignment]

    spec = build_event_spec("lax", lax, owner=Owner)

    assert spec.payload_model.model_validate({}).n == 5

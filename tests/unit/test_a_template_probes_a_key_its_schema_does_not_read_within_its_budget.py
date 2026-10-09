"""A settings key the schema cannot vouch for is probed within its budget; one it reads is not.

A field read back by a validator rather than by the schema is accepted once pydantic, given a
different valid value under the written key, reads that value back (a bool, a float, an int or a
literal next to the one held, a list or a mapping, or beside a `Json` field). The probe tries the
first three scalars only, and takes the first value pydantic accepts as its answer. It runs within
the budget docs/service-templates.md states: a document that costs exactly its budget is accepted
and one unit over is refused, up to the 4096-unit ceiling. A key the schema reads, through a
one-step `AliasPath` or `AliasChoices` on a dataclass field, or a pydantic dataclass with a before
validator, is accepted without a probe.
"""

import dataclasses
import json
from typing import Annotated, Literal

import pytest
from pydantic import (
    AfterValidator,
    AliasChoices,
    AliasPath,
    BaseModel,
    Field,
    Json,
    StrictBool,
    create_model,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass

from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import TemplateError
from tests.fixtures.shipment_templates import shipment_template

pytestmark = pytest.mark.unit

NO_KEY = "settings field {!r} must round-trip through an accepted serialized key"


def registered(model):
    return TemplateCatalog().register(shipment_template(settings_model=model))


def refusal(model, document) -> str:
    with pytest.raises(TemplateError) as refused:
        registered(model).normalize(document)
    return str(refused.value)


class ReadFromItsAlias(BaseModel):
    """`x` is written as `xAlias` and read back from it by a validator, not by the schema, so the
    check asks pydantic (the probe) before it accepts the field."""

    @model_validator(mode="before")
    @classmethod
    def _remap(cls, data):
        if isinstance(data, dict) and "xAlias" in data:
            return {**{k: v for k, v in data.items() if k != "xAlias"}, "x": data["xAlias"]}
        return data


def probed(annotation, default, **others):
    return create_model(
        "Probed",
        __base__=ReadFromItsAlias,
        x=(annotation, Field(default=default, serialization_alias="xAlias")),
        **others,
    )


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (StrictBool, True),
        (float, 1.5),
        (Annotated[int, Field(ge=5, le=6)], 5),
        (Annotated[int, Field(ge=4, le=5)], 5),
        (Literal[5, 7], 5),
        (Literal["x", "y"], "x"),
        (Literal["x", "y"], "y"),
        (list[int], [1]),
        (list[int | None], [None, 1]),
        (dict[str, int], {"a": 1}),
    ],
    ids=[
        "strict-bool",
        "float",
        "int-only-one-above",
        "int-only-one-below",
        "int-only-two-above",
        "str-x-or-y-at-x",
        "str-x-or-y-at-y",
        "list",
        "list-with-a-null",
        "dict",
    ],
)
def test_a_probed_field_with_some_other_valid_value_is_accepted(annotation, value):
    model = probed(annotation, value)
    document = {"xAlias": value}
    assert model.model_validate(document).x == value, "the case is not one pydantic reads back"

    accepted = registered(model).normalize(document)

    assert accepted.materialize() == model.model_validate(document)
    assert json.loads(accepted._json) == document


def test_a_probed_value_is_tried_at_its_first_three_scalars_only():
    model = probed(tuple[Literal["a"], Literal["a"], Literal["a"], int], ("a", "a", "a", 1))
    document = {"xAlias": ["a", "a", "a", 1]}
    assert model.model_validate(document).x == ("a", "a", "a", 1)

    assert refusal(model, document) == NO_KEY.format("x")


def test_a_probed_scalar_is_read_once_with_a_different_value():
    """The first different value pydantic accepts, 6, is clamped back to 5 and moves nothing; the
    probe takes that read as the scalar's answer and does not go on to 4."""
    model = probed(Annotated[int, AfterValidator(lambda v: min(v, 5))], 5)
    assert model.model_validate({"xAlias": 4}).x == 4

    assert refusal(model, {"xAlias": 5}) == NO_KEY.format("x")


def test_a_probed_field_beside_a_json_field_is_accepted():
    model = probed(int, 1, j=(Json[dict[str, int]], '{"a": 1}'))
    document = {"j": '{"a":1}', "xAlias": 3}

    accepted = registered(model).normalize(document)

    assert accepted.materialize().x == 3
    assert accepted.materialize().j == {"a": 1}


# The budget (docs/service-templates.md): 256 units plus 8 per scalar, at most 4096; a copy costs
# 1, a validation and a dump of the field set alone 1 plus 1 per 4096 bytes of the document's JSON.
# One probed int field read at the first value tried costs 1 + 2 * (1 + bytes / 4096), which is
# 3 + bytes / 2048.


def padded():
    return probed(int, 1, pad=(list[str], []))


def document_of_length(length, items):
    """`{"pad": [big, "", ...], "xAlias": 1}`: `items` strings in `pad`, `length` bytes of JSON."""
    base = {"pad": [""] * items, "xAlias": 1}
    size = len(json.dumps(base))
    return {"pad": ["a" * (length - size)] + [""] * (items - 1), "xAlias": 1}


def test_a_probe_that_costs_exactly_its_budget_is_accepted():
    # 4 scalars: 256 + 32 = 288 units = 3 + bytes / 2048 at 583,680 bytes.
    document = document_of_length(583_680, 3)
    assert len(json.dumps(document)) == 583_680

    assert registered(padded()).normalize(document).materialize().x == 1


def test_a_probe_that_costs_just_over_its_budget_is_refused():
    document = document_of_length(583_681, 3)
    assert len(json.dumps(document)) == 583_681

    assert refusal(padded(), document) == NO_KEY.format("x")


def test_a_probe_that_costs_just_over_the_largest_budget_is_refused():
    # 501 scalars would give 4264 units; the budget stops at 4096 = 3 + bytes / 2048 at 8,382,464.
    document = document_of_length(8_382_465, 500)
    assert len(json.dumps(document)) == 8_382_465

    assert refusal(padded(), document) == NO_KEY.format("x")


# A key the schema reads is accepted without the probe. These fields have a single allowed value,
# for which the probe can build no different one, so only the schema can accept them.


@dataclasses.dataclass
class AliasedLeaf:
    by_path: Annotated[
        Literal["only"], Field(validation_alias=AliasPath("pathKey"), serialization_alias="pathKey")
    ] = "only"
    by_choice: Annotated[
        Literal["only"],
        Field(validation_alias=AliasChoices("choiceKey", "other"), serialization_alias="choiceKey"),
    ] = "only"


def test_a_dataclass_field_read_through_a_one_step_alias_path_or_alias_choices_is_accepted():
    model = create_model("HoldsAliasedLeaf", leaf=(AliasedLeaf, AliasedLeaf()))
    document = {"leaf": {"pathKey": "only", "choiceKey": "only"}}

    accepted = registered(model).normalize(document)

    assert json.loads(accepted._json) == document


@pydantic_dataclass
class CheckedBefore:
    kind: Literal["only"] = "only"

    @model_validator(mode="before")
    @classmethod
    def _keep(cls, data):
        return data


def test_a_pydantic_dataclass_with_a_before_validator_is_accepted():
    model = create_model("HoldsChecked", leaf=(CheckedBefore, CheckedBefore()))
    document = {"leaf": {"kind": "only"}}

    assert json.loads(registered(model).normalize(document)._json) == document

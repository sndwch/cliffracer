"""A template's settings are compared by value when they are normalized, whatever their `==` says.

`normalize` stores the settings as JSON and reads them back, and refuses settings that do not read
back the same. A dataclass declared `eq=False` compares by identity, and the copy read back is a new
object, so `==` would refuse every settings model holding one although its document reads back.
The comparison is by value (`_same`): a dataclass of one class by its fields. Settings whose value
does not read back are still refused, also when the document it writes does.
"""

import dataclasses

import pytest
from pydantic import BaseModel, Field, field_serializer

from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import TemplateError
from tests.fixtures.shipment_templates import shipment_template

pytestmark = pytest.mark.unit


def _registered(model):
    return TemplateCatalog().register(shipment_template(settings_model=model))


@dataclasses.dataclass(eq=False)
class Plain:
    n: int = 1


class HoldsPlain(BaseModel):
    p: Plain = Field(default_factory=Plain)


@pytest.mark.parametrize("given", ["instance", "mapping"])
def test_settings_holding_an_eq_false_dataclass_are_normalized(given):
    settings = HoldsPlain(p=Plain(n=7))

    normalized = _registered(HoldsPlain).normalize(
        settings if given == "instance" else settings.model_dump()
    )

    assert normalized.materialize().p.n == 7


@dataclasses.dataclass
class Counted:
    n: int = 1


class HoldsCounted(BaseModel):
    p: Counted = Field(default_factory=Counted)


def test_CONTROL_settings_holding_a_dataclass_with_eq_are_normalized():
    assert _registered(HoldsCounted).normalize(HoldsCounted(p=Counted(n=7))).materialize().p.n == 7


class Hides(BaseModel):
    """The dataclass is written as `{"n": 1}` whatever it holds: the document reads back as a
    dataclass holding 1, which writes the same document, so only a comparison by value sees it."""

    p: Plain = Field(default_factory=Plain)

    @field_serializer("p")
    def _always_one(self, value: Plain) -> dict[str, int]:
        return {"n": 1}


@pytest.mark.parametrize("given", ["instance", "mapping"])
def test_settings_whose_value_does_not_read_back_are_refused(given):
    """Given as a mapping, the comparison of the stored copy with the validated settings is the
    only check that sees the value read back is not the value given."""
    with pytest.raises(TemplateError):
        _registered(Hides).normalize(
            Hides(p=Plain(n=7)) if given == "instance" else {"p": {"n": 7}}
        )

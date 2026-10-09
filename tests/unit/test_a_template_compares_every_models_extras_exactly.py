"""A template's settings are refused when a model's extra at any depth does not read back as itself.

An extra has no declared type to be read back as, so a set or a dataclass held in one comes back
from the stored document as a list or a dict. The declared fields are compared by value (an
`eq=False` dataclass by its fields); the extras are compared exactly, at the top level and in a
nested model held by a field or in a list, a tuple or a dict. An extra that reads back as itself is
accepted wherever it is.
"""

import dataclasses

import pytest
from pydantic import BaseModel, ConfigDict, Field

from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import TemplateError
from tests.fixtures.shipment_templates import shipment_template

pytestmark = pytest.mark.unit


def _registered(model):
    return TemplateCatalog().register(shipment_template(settings_model=model))


@dataclasses.dataclass(frozen=True)
class Boxed:
    n: int


@dataclasses.dataclass(eq=False)
class Plain:
    n: int = 1


class Inner(BaseModel):
    model_config = ConfigDict(extra="allow")
    x: int = 1
    p: Plain = Field(default_factory=Plain)


class Top(BaseModel):
    model_config = ConfigDict(extra="allow")
    x: int = 1


class InAField(BaseModel):
    inner: Inner


class InAList(BaseModel):
    inner: list[Inner]


class InATuple(BaseModel):
    inner: tuple[Inner, ...]


class InADict(BaseModel):
    inner: dict[str, Inner]


A_SET = frozenset({Boxed(1)})


@pytest.mark.parametrize(
    ("model", "settings"),
    [
        (Top, lambda: Top(note=A_SET)),
        (InAField, lambda: InAField(inner=Inner(note=A_SET))),
        (InAList, lambda: InAList(inner=[Inner(), Inner(note=A_SET)])),
        (InATuple, lambda: InATuple(inner=(Inner(), Inner(note=A_SET)))),
        (InADict, lambda: InADict(inner={"a": Inner(), "b": Inner(note=A_SET)})),
    ],
    ids=[
        "top-level",
        "nested-in-a-field",
        "nested-in-a-list",
        "nested-in-a-tuple",
        "nested-in-a-dict",
    ],
)
def test_an_extra_that_does_not_read_back_is_refused_at_any_depth(model, settings):
    with pytest.raises(TemplateError, match="round-trip"):
        _registered(model).normalize(settings())


@pytest.mark.parametrize(
    ("model", "settings"),
    [
        (Top, lambda: Top(note=3)),
        (InAField, lambda: InAField(inner=Inner(note=3))),
        (InAList, lambda: InAList(inner=[Inner(note=3)])),
        (InATuple, lambda: InATuple(inner=(Inner(note=3),))),
        (InADict, lambda: InADict(inner={"b": Inner(note=3)})),
    ],
    ids=[
        "top-level",
        "nested-in-a-field",
        "nested-in-a-list",
        "nested-in-a-tuple",
        "nested-in-a-dict",
    ],
)
def test_CONTROL_an_extra_that_reads_back_is_accepted_at_any_depth(model, settings):
    _registered(model).normalize(settings())


def test_an_eq_false_dataclass_inside_a_nested_model_is_accepted():
    normalized = _registered(InAField).normalize(InAField(inner=Inner(p=Plain(n=7))))

    assert normalized.materialize().inner.p.n == 7

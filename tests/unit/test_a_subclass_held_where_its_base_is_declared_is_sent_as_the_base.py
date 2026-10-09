"""A model subclass held where its base is declared is sent as the base.

Pydantic writes a field by its declared type, so a `Sub(Item)` in a field, list or dict declared as
`Item` is written with `Item`'s fields only: the fields `Sub` adds are not on the wire, and a
receiver that declares `Item` could not hold them. Nothing is refused or warned about. The argument
itself is the declaration the method makes, and a subclass instance passed as the argument is sent
whole.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from cliffracer.core.validation import wire_models

pytestmark = pytest.mark.unit


class Item(BaseModel):
    n: int


class Sub(Item):
    extra: int = 0


class InAField(BaseModel):
    item: Item


class InAList(BaseModel):
    items: list[Item]


class InADict(BaseModel):
    by_key: dict[str, Item]


SUB = Sub(n=1, extra=2)


@pytest.mark.parametrize(
    ("value", "sent"),
    [
        pytest.param(InAField(item=SUB), {"item": {"n": 1}}, id="a-field"),
        pytest.param(InAList(items=[SUB]), {"items": [{"n": 1}]}, id="a-list"),
        pytest.param(InADict(by_key={"k": SUB}), {"by_key": {"k": {"n": 1}}}, id="a-dict"),
    ],
)
def test_a_subclass_held_where_its_base_is_declared_is_sent_as_the_base(value, sent):
    assert wire_models(value) == sent


def test_a_subclass_passed_as_the_argument_itself_is_sent_whole():
    assert wire_models(SUB) == {"n": 1, "extra": 2}

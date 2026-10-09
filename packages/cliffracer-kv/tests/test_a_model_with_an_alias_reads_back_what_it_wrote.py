"""A Pydantic model written by `put()` reads back through `get(as_type=Model)`.

A model's JSON is written under its field names and validated under its aliases, so a model with a
validation alias was stored and then refused on the way out. The write stores the field names when
the model reads them back as itself, else the aliases when it reads those back as itself, else each
field where its validation alias reads it, and every model that read its field names back has the
bytes it had. A model that reads back no form as itself is refused
(`test_a_model_is_stored_only_in_a_form_it_reads_back_as_itself.py`).
"""

from __future__ import annotations

import json
from typing import Annotated

import pytest
from cliffracer_kv import ModelDoesNotReadBackError
from cliffracer_kv.serialization import deserialize_value, serialize_value
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_serializer,
)

pytestmark = pytest.mark.unit


class Plain(BaseModel):
    user_id: str
    count: int = 0


class Aliased(BaseModel):
    user_id: str = Field(alias="userId")


class SerializationOnly(BaseModel):
    user_id: str = Field(serialization_alias="userId")


class Both(BaseModel):
    user_id: str = Field(validation_alias="uid", serialization_alias="userId")


class ByNameToo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    user_id: str = Field(alias="userId")


class Child(BaseModel):
    item_id: int = Field(alias="itemId")


class Parent(BaseModel):
    child: Child
    items: list[Child] = []


class Choices(BaseModel):
    user_id: str = Field(
        validation_alias=AliasChoices("userId", "uid"), serialization_alias="userId"
    )


class Path(BaseModel):
    user_id: str = Field(validation_alias=AliasPath("data", "id"))


class Written(BaseModel):
    user_id: str = Field(validation_alias=AliasChoices("user_id", "uid"), serialization_alias="uid")


class Forbidding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def doubled(self) -> int:
        return self.n * 2


class Suffixed(BaseModel):
    name: str

    @field_serializer("name")
    def _suffix(self, value: str) -> str:
        return value + "!"


def read_back(value: BaseModel) -> BaseModel:
    return deserialize_value(serialize_value(value), as_type=type(value))


def test_a_model_with_a_validation_alias_reads_back_equal():
    value = Aliased(userId="u1")

    assert read_back(value) == value
    assert json.loads(serialize_value(value)) == {"userId": "u1"}


def test_a_nested_aliased_model_reads_back_equal():
    value = Parent(child=Child(itemId=1), items=[Child(itemId=2), Child(itemId=3)])

    assert read_back(value) == value
    assert json.loads(serialize_value(value))["child"] == {"itemId": 1}


def test_alias_choices_that_name_a_form_the_model_writes_read_back():
    value = Written(user_id="u1")

    assert read_back(value) == value
    assert serialize_value(value) == b'{"user_id":"u1"}'


def test_alias_choices_that_the_model_validates_only_under_its_serialization_alias_read_back():
    value = Choices(uid="u1")

    assert read_back(value) == value
    assert json.loads(serialize_value(value)) == {"userId": "u1"}


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (Path.model_construct(user_id="u1"), b'{"data":{"id":"u1"}}'),
        (Both.model_construct(user_id="u1"), b'{"uid":"u1"}'),
    ],
    ids=["alias-path", "different-in-and-out"],
)
def test_a_model_that_validates_neither_dump_is_stored_where_its_validation_alias_reads(
    value, stored
):
    assert serialize_value(value) == stored
    assert read_back(value) == value


def test_a_model_that_validates_no_form_is_refused():
    with pytest.raises(ModelDoesNotReadBackError, match="Forbidding"):
        serialize_value(Forbidding(n=1))


def test_a_model_that_does_not_re_dump_what_it_read_is_refused():
    with pytest.raises(ModelDoesNotReadBackError, match="Suffixed"):
        serialize_value(Suffixed(name="x"))


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (Plain(user_id="u1", count=2), b'{"user_id":"u1","count":2}'),
        (SerializationOnly(user_id="u1"), b'{"user_id":"u1"}'),
        (ByNameToo(userId="u1"), b'{"user_id":"u1"}'),
    ],
    ids=["no-alias", "serialization-alias-only", "populate-by-name"],
)
def test_a_model_that_already_read_back_is_stored_byte_for_byte_as_before(value, stored):
    assert serialize_value(value) == stored
    assert read_back(value) == value


def test_a_nested_model_inside_a_dict_or_list_is_stored_under_its_alias():
    value = {"one": Child(itemId=1), "many": [Child(itemId=2)]}

    assert json.loads(serialize_value(value)) == {"one": {"itemId": 1}, "many": [{"itemId": 2}]}


def test_CONTROL_the_plain_dump_of_an_aliased_model_does_not_read_back():
    """Without this, "reads back" could mean "the model has no alias to disagree about"."""
    plain = Aliased(userId="u1").model_dump_json().encode()

    with pytest.raises(Exception, match="userId"):
        deserialize_value(plain, as_type=Aliased)


def test_a_model_annotated_alias_reads_back_equal():
    class Annotated_(BaseModel):
        user_id: Annotated[str, Field(alias="userId")]

    value = Annotated_(userId="u1")

    assert read_back(value) == value

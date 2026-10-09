"""A write refuses a secret wherever the dump would write its mask, not only inside a pydantic model.

`serialize_value` walked models, dicts, lists, tuples and sets, so a `SecretStr` inside a dataclass,
standard or pydantic, was written as `**********` with no error, as were one held as an extra of a
model that allows them and one a computed field returns. Each of those is refused now, naming where
the secret is, and a dataclass field declared `exclude=True` stays out of the dump as it was.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from cliffracer_kv.serialization import serialize_value
from pydantic import BaseModel, ConfigDict, Field, SecretBytes, SecretStr, computed_field
from pydantic.dataclasses import dataclass as pydantic_dataclass

pytestmark = pytest.mark.unit


@dataclasses.dataclass
class Login:
    user: str
    pw: SecretStr


@pydantic_dataclass
class PydanticLogin:
    user: str
    pw: SecretStr


@dataclasses.dataclass
class Keyring:
    keys: list[SecretBytes]


@dataclasses.dataclass
class Account:
    login: Login


class Holder(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    login: Login


class Open(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str


class Derived(BaseModel):
    name: str

    @computed_field
    @property
    def pw(self) -> SecretStr:
        return SecretStr("s3cr3t")


@pytest.mark.parametrize(
    ("value", "where"),
    [
        (Login("u", SecretStr("s3cr3t")), r"Login\.pw"),
        (PydanticLogin(user="u", pw=SecretStr("s3cr3t")), r"PydanticLogin\.pw"),
        (Keyring([SecretBytes(b"k")]), r"Keyring\.keys\[0\]"),
        (Account(Login("u", SecretStr("s3cr3t"))), r"Account\.login\.pw"),
        (Holder(login=Login("u", SecretStr("s3cr3t"))), r"Holder\.login\.pw"),
        ([Login("u", SecretStr("s3cr3t"))], r"list\[0\]\.pw"),
        ({"k": Login("u", SecretStr("s3cr3t"))}, r"dict\['k'\]\.pw"),
        ({"k": [Account(Login("u", SecretStr("s3cr3t")))]}, r"dict\['k'\]\[0\]\.login\.pw"),
        (Open(name="n", pw=SecretStr("s3cr3t")), r"Open\.pw"),
        (Derived(name="n"), r"Derived\.pw"),
    ],
    ids=[
        "dataclass",
        "pydantic-dataclass",
        "list-in-dataclass",
        "dataclass-in-dataclass",
        "dataclass-in-model",
        "dataclass-in-list",
        "dataclass-in-dict",
        "dataclass-in-list-in-dict",
        "model-extra",
        "computed-field",
    ],
)
def test_a_secret_in_a_value_the_dump_would_mask_is_refused_and_its_place_named(value, where):
    with pytest.raises(TypeError) as caught:
        serialize_value(value)

    message = str(caught.value)
    assert where.replace("\\", "") in message, message
    assert "get_secret_value()" in message and "s3cr3t" not in message, message


def test_a_dataclass_field_declared_excluded_stays_out_of_the_dump():
    @pydantic_dataclass
    class Quiet:
        name: str
        pw: SecretStr = Field(exclude=True)

    assert json.loads(serialize_value(Quiet(name="n", pw=SecretStr("x")))) == {"name": "n"}


def test_the_secret_value_passed_on_purpose_is_stored_from_a_dataclass():
    login = Login("u", SecretStr("s3cr3t"))

    stored = serialize_value(Login(login.user, login.pw.get_secret_value()))  # type: ignore[arg-type]

    assert json.loads(stored) == {"user": "u", "pw": "s3cr3t"}


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (Login("u", "p"), {"user": "u", "pw": "p"}),  # type: ignore[arg-type]
        (Account(Login("u", "p")), {"login": {"user": "u", "pw": "p"}}),  # type: ignore[arg-type]
        ([Keyring([])], [{"keys": []}]),
        (Open(name="n", other=1), {"name": "n", "other": 1}),
    ],
    ids=["dataclass", "nested-dataclass", "dataclass-in-list", "model-extra"],
)
def test_CONTROL_a_dataclass_or_extra_without_a_secret_is_stored_as_before(value, stored):
    """Without this, "refuses a secret" could mean "refuses every dataclass"."""
    assert json.loads(serialize_value(value)) == stored


def test_a_dataclass_that_refers_to_itself_does_not_loop():
    @dataclasses.dataclass
    class Node:
        label: str
        child: Node | None = None

    node = Node("a")
    node.child = node

    # The dump itself cannot serialize a cycle; what matters is that the secret walk returns.
    with pytest.raises(TypeError, match="Circular reference"):
        serialize_value(node)

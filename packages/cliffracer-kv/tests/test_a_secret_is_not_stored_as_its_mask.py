"""A write refuses a `SecretStr` or `SecretBytes`, where it used to store the mask in its place.

A secret's dump is `**********`, so a model holding one was written without the secret and read back
as a model whose secret is the mask, with no error anywhere. A bucket is also readable by every
client with access to it, so storing the real secret implicitly is not the answer either. The write
raises `TypeError` naming the place of the secret and pointing to `get_secret_value()` for a caller
who means to store it. A `Field(exclude=True)` field is not part of the dump and stays as it is.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from cliffracer_kv.serialization import serialize_value
from pydantic import BaseModel, Field, SecretBytes, SecretStr

pytestmark = pytest.mark.unit


class Login(BaseModel):
    user: str
    pw: SecretStr


class Account(BaseModel):
    login: Login


class Anything(BaseModel):
    anything: Any


def test_a_model_with_a_secret_field_is_refused_naming_the_field_and_the_remedy():
    with pytest.raises(TypeError) as caught:
        serialize_value(Login(user="u", pw="s3cr3t"))

    message = str(caught.value)
    assert "Login.pw" in message, message
    assert "SecretStr" in message, message
    assert "get_secret_value()" in message, message
    assert "s3cr3t" not in message, message


def test_a_secret_bytes_field_is_refused_too():
    class Blob(BaseModel):
        key: SecretBytes

    with pytest.raises(TypeError, match=r"Blob\.key"):
        serialize_value(Blob(key=b"k"))


def test_a_bare_secret_is_refused():
    with pytest.raises(TypeError, match="SecretStr"):
        serialize_value(SecretStr("s3cr3t"))


@pytest.mark.parametrize(
    ("value", "where"),
    [
        (Account(login=Login(user="u", pw="x")), r"Account\.login\.pw"),
        ({"logins": [Login(user="u", pw="x")]}, r"dict\['logins'\]\[0\]\.pw"),
        ([{"k": SecretStr("x")}], r"list\[0\]\['k'\]"),
        ((SecretStr("x"),), r"tuple\[0\]"),
        (Anything(anything={"a": [SecretStr("x")]}), r"Anything\.anything\['a'\]\[0\]"),
    ],
    ids=["nested-model", "list-in-dict", "dict-in-list", "tuple", "any-typed-field"],
)
def test_a_secret_at_any_depth_is_refused_and_its_place_is_named(value, where):
    with pytest.raises(TypeError, match=where):
        serialize_value(value)


def test_the_secret_value_passed_on_purpose_is_stored():
    stored = serialize_value({"pw": Login(user="u", pw="s3cr3t").pw.get_secret_value()})

    assert json.loads(stored) == {"pw": "s3cr3t"}


def test_an_excluded_secret_field_stays_out_of_the_dump_as_its_author_declared():
    """Not refused as a secret, and not compared on the way back, whatever it holds: the model
    declares it unstored. (A required excluded field cannot be read back at all, so a model with
    one is refused as unreadable.)"""

    class Quiet(BaseModel):
        name: str
        pw: SecretStr = Field(default=SecretStr(""), exclude=True)

    assert json.loads(serialize_value(Quiet(name="n", pw="x"))) == {"name": "n"}


def test_a_required_excluded_field_makes_the_dump_unreadable_so_the_model_is_refused():
    """The dump leaves the field out and the class requires it, so `get` could not read it back."""
    from cliffracer_kv import ModelDoesNotReadBackError

    class Quiet(BaseModel):
        name: str
        pw: SecretStr = Field(exclude=True)

    with pytest.raises(ModelDoesNotReadBackError, match="Quiet"):
        serialize_value(Quiet(name="n", pw="x"))


def test_CONTROL_a_model_without_a_secret_is_stored_as_before():
    """Without this, "refuses a secret" could mean "refuses every model"."""

    class Public(BaseModel):
        login: str | None = None

    assert serialize_value(Public(login=None)) == b'{"login":null}'
    assert serialize_value(Anything(anything={"a": [1, "b"]})) == b'{"anything":{"a":[1,"b"]}}'


def test_a_model_that_refers_to_itself_does_not_loop():
    class Node(BaseModel):
        label: str
        child: Node | None = None

    node = Node(label="a")
    node.child = node

    # The dump itself cannot serialize a cycle; what matters is that the secret walk returns.
    with pytest.raises(ValueError):
        serialize_value(node)

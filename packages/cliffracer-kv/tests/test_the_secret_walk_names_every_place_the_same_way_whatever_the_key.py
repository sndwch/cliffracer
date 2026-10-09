"""The secret walk skips only what cannot hold a secret, and names a place the same way for any key.

A value of an exact builtin scalar type (str, bytes, bytearray, int, float, bool, None) holds
nothing, so the walk passes over it, and an exact dict, list, tuple or set is walked by its members
alone. A subclass of any of those may be a dataclass or anything else, so it is walked like any
other object. A refusal names the place with the repr of each key on the way, whatever the key's
type, and a key whose repr raises makes the write raise that error.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from cliffracer_kv.serialization import serialize_value
from pydantic import SecretBytes, SecretStr

pytestmark = pytest.mark.unit


@dataclasses.dataclass
class Tagged(int):
    """An int that is also a dataclass, with a field of its own."""

    note: Any = None


@dataclasses.dataclass
class Labelled(dict):
    """A dict that is also a dataclass: its fields are walked, as a dataclass's are."""

    note: Any = None


def tagged(note: Any) -> Tagged:
    value = Tagged(3)
    value.note = note
    return value


def labelled(note: Any) -> Labelled:
    value = Labelled()
    value.note = note
    return value


@pytest.mark.parametrize("build", [tagged, labelled], ids=["int-subclass", "dict-subclass"])
def test_a_dataclass_that_subclasses_a_builtin_is_walked_and_its_secret_refused(build):
    with pytest.raises(TypeError) as caught:
        serialize_value({"n": build(SecretStr("s3cr3t"))})

    message = str(caught.value)
    assert "(dict['n'].note)" in message and "s3cr3t" not in message, message


def test_CONTROL_the_same_dataclass_without_a_secret_is_stored():
    """Without this, the refusal above could be a refusal of every int subclass."""
    assert serialize_value({"n": tagged("plain")}) == b'{"n": 3}'


def test_a_refusal_spells_every_key_on_the_way_as_its_repr():
    value = {1: [{"k": {b"b": {None: {2.5: {True: {(1, "t"): SecretBytes(b"s3cr3t")}}}}}}]}

    with pytest.raises(TypeError) as caught:
        serialize_value(value)

    message = str(caught.value)
    assert "(dict[1][0]['k'][b'b'][None][2.5][True][(1, 't')])" in message, message


class LoudKey:
    def __repr__(self) -> str:
        raise RuntimeError("repr of LoudKey")


@pytest.mark.parametrize(
    ("key", "error"),
    [(LoudKey(), RuntimeError), (10**5000, ValueError)],
    ids=["repr-raises", "int-past-the-repr-limit"],
)
def test_a_key_whose_repr_raises_makes_the_write_raise_that_error(key, error):
    with pytest.raises(error):
        serialize_value({"outer": {key: "v"}})

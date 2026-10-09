"""A generic `Secret[...]` and a secret used as a dict key are refused the way a `SecretStr` is.

The walk refused `SecretStr` and `SecretBytes` only, so pydantic's generic `Secret[int]` was stored as
its mask `**********`, and it walked a dict's values but not its keys, so a model holding
`dict[SecretStr, int]` was stored with the mask as the key. A plain dict with a secret key was refused,
but as a value with no JSON form, naming no secret. Each is now the named secret refusal.
"""

import json

import pytest
from cliffracer_kv.serialization import serialize_value
from pydantic import BaseModel, ConfigDict, Secret, SecretBytes, SecretStr

pytestmark = pytest.mark.unit


class Pin(BaseModel):
    pin: Secret[int]


class Keyed(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    by_secret: dict[SecretStr, int]


@pytest.mark.parametrize(
    ("value", "kind", "where"),
    [
        (Pin(pin=1234), "Secret", "Pin.pin"),
        (Secret[int](1234), "Secret", "Secret"),
        ({"a": [Secret[str]("s3cr3t")]}, "Secret", "dict['a'][0]"),
        (Keyed(by_secret={SecretStr("s3cr3t"): 1}), "SecretStr", "Keyed.by_secret key"),
        ({SecretStr("s3cr3t"): 1}, "SecretStr", "dict key"),
        ({"k": {SecretBytes(b"s3cr3t"): 1}}, "SecretBytes", "dict['k'] key"),
    ],
    ids=[
        "generic-field",
        "generic-alone",
        "generic-in-a-list-in-a-dict",
        "secret-key-in-a-model",
        "secret-key-in-a-dict",
        "secret-bytes-key-nested",
    ],
)
def test_the_secret_is_refused_naming_its_kind_and_place(value, kind, where):
    with pytest.raises(TypeError) as caught:
        serialize_value(value)

    message = str(caught.value)
    assert f"cannot store a {kind} " in message, message
    assert f"({where})" in message, message
    assert "get_secret_value()" in message and "s3cr3t" not in message and "1234" not in message


def test_CONTROL_a_dict_with_ordinary_keys_and_a_generic_value_passed_on_purpose_is_stored():
    pin = Secret[int](1234)

    assert json.loads(serialize_value({"pin": pin.get_secret_value()})) == {"pin": 1234}

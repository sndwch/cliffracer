"""Property: a KV write refuses a value holding a secret anywhere, and stores any other value.

Values are generated from a seed: nested lists, tuples, dicts keyed by strings and ints, models
with extras or a computed field, and stdlib and Pydantic dataclasses, about half of them holding a
`SecretStr`, `SecretBytes` or `Secret[int]` at some depth (`tests.fixtures.properties.kv_values`).
Every value holding a secret must be refused with the error that names the secret, and every other
value must be stored and read back as its JSON form. A secret in a field declared `exclude=True` is
not stored, so the value holding it is stored. No limit is known: every finding fails.

The CONTROL runs the same values with the secret check skipped, and must find planted values stored.
"""

import dataclasses
import json
import random
from typing import Any

import pytest
from cliffracer_kv import serialization
from cliffracer_kv.serialization import deserialize_value, serialize_value
from pydantic import BaseModel, SecretStr
from pydantic_core import to_jsonable_python

from tests.fixtures.properties import (
    Finding,
    assert_control_finds,
    assert_only_known_limits,
    cases,
    seeds,
)
from tests.fixtures.properties.kv_values import generate

pytestmark = pytest.mark.unit

SEED = 20261004
CASES = 5000
CONTROL_CASES = 400
#: A third of what the CONTROL found when measured on seed 20261004: 56 planted values stored of 400.
CONTROL_FLOOR = 18


def findings(count: int = CASES) -> list[Finding]:
    found: list[Finding] = []
    for seed in seeds(SEED):
        rng = random.Random(seed)
        for index in range(cases(count)):
            value, planted = generate(rng)
            try:
                stored = serialize_value(value)
            except TypeError as refused:
                if planted and str(refused).startswith("cannot store a Secret"):
                    continue
                what = (
                    f"a value holding {planted} secret(s) was refused with another error"
                    if planted
                    else "a value holding no secret was refused"
                )
                found.append(Finding(seed, index, f"{what}: {refused}", repr(value)))
                continue
            if planted:
                found.append(
                    Finding(
                        seed, index, f"a value holding {planted} secret(s) was stored", repr(value)
                    )
                )
                continue
            back = deserialize_value(stored)
            if back != json.loads(json.dumps(to_jsonable_python(value))):
                found.append(
                    Finding(seed, index, f"a clean value read back as {back!r}", repr(value))
                )
    return found


def test_a_kv_write_refuses_every_planted_secret_and_keeps_every_clean_value():
    assert_only_known_limits(findings(), [], check="K (KV secrets)")


def test_CONTROL_with_the_secret_check_skipped_planted_values_are_stored(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(serialization, "_refuse_a_secret", lambda value, *_: calls.append(value))

    found = findings(CONTROL_CASES)
    stored = [f for f in found if "was stored" in f.what]

    assert len(calls) == CONTROL_CASES, "the skipped check was not the one every write calls"
    assert_control_finds(stored, at_least=CONTROL_FLOOR, control="secret check skipped")


@dataclasses.dataclass
class Pair:
    a: Any = None
    b: Any = None


class Holder(BaseModel):
    a: Any = None
    b: Any = None


def _a_cyclic_list():
    value: list[Any] = []
    value.append(value)
    value.append(SecretStr("pw"))
    return value


def _a_cyclic_dict():
    value: dict[str, Any] = {}
    value["self"] = value
    value["z"] = SecretStr("pw")
    return value


def _a_cyclic_dataclass():
    value = Pair()
    value.a = value
    value.b = SecretStr("pw")
    return value


def _a_cyclic_model():
    value = Holder()
    value.a = value
    value.b = SecretStr("pw")
    return value


@pytest.mark.parametrize(
    "build",
    [_a_cyclic_list, _a_cyclic_dict, _a_cyclic_dataclass, _a_cyclic_model],
    ids=["list", "dict", "dataclass", "model"],
)
def test_a_cyclic_value_holding_a_secret_is_refused_by_name(build):
    """The walk remembers each container it has entered, so a value that holds itself is walked once
    and the secret beside the cycle is still found and named."""
    with pytest.raises(TypeError, match="^cannot store a SecretStr"):
        serialize_value(build())

"""The constraints a signature hash carries are tied to pydantic's own list, and each one counts.

`signature_hash` is what a generated client compares to notice that a method changed under it.
It carries only the `Field` constraints named in `CONSTRAINT_ATTRS`; one pydantic has and that
list lacks is dropped from the type, so two versions of a method differing only in it hash the
same and the drift check cannot see the change. `allow_inf_nan` was such a constraint: a float
that accepts `nan` and one that rejects it were the same method to the client.

The cases below were a hand-copied list nothing tied to `CONSTRAINT_ATTRS`, so a twelfth
attribute would have been silently uncovered. They are now keyed by attribute, the keys must equal
`CONSTRAINT_ATTRS` exactly, and `CONSTRAINT_ATTRS` plus a short, reasoned exclusion list must equal
the constraints pydantic itself knows (`FieldInfo.metadata_lookup`), so a constraint pydantic adds
has to be put on one side or the other.
"""

from typing import Annotated

import pytest
from pydantic import Field
from pydantic.fields import FieldInfo

from cliffracer import CliffracerService, rpc
from cliffracer.core.typed_rpc import CONSTRAINT_ATTRS
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit

#: attr -> (field one, field two, parameter type). Two fields that differ only in `attr`.
CASES = {
    "ge": (Field(ge=1), Field(ge=10), int),
    "gt": (Field(gt=0), Field(gt=5), int),
    "le": (Field(le=100), Field(le=50), int),
    "lt": (Field(lt=10), Field(lt=8), int),
    "min_length": (Field(min_length=2), Field(min_length=5), str),
    "max_length": (Field(max_length=20), Field(max_length=10), str),
    "pattern": (Field(pattern=r"^[a-z]+$"), Field(pattern=r"^[A-Z]+$"), str),
    "strict": (Field(strict=True), Field(strict=False), int),
    "multiple_of": (Field(multiple_of=2), Field(multiple_of=3), int),
    "max_digits": (Field(max_digits=5), Field(max_digits=8), float),
    "decimal_places": (Field(decimal_places=2), Field(decimal_places=4), float),
    "allow_inf_nan": (Field(allow_inf_nan=True), Field(allow_inf_nan=False), float),
    "coerce_numbers_to_str": (
        Field(coerce_numbers_to_str=True),
        Field(coerce_numbers_to_str=False),
        str,
    ),
}

#: Constraints pydantic knows that the hash deliberately leaves out, and why.
EXCLUDED = {
    "fail_fast": "changes how many errors a validation reports, not what is accepted",
    "union_mode": "applies to unions, which a typed signature does not support",
}


def _signature_hash(field, tp) -> str:
    class Svc(CliffracerService):
        @rpc
        async def action(self, val: Annotated[tp, field]) -> int:  # type: ignore[valid-type]
            return 1

    return describe(Svc, service="svc", version="1").methods[0].signature_hash


def test_the_cases_cover_exactly_the_constraints_the_hash_carries():
    assert set(CASES) == set(CONSTRAINT_ATTRS), set(CASES) ^ set(CONSTRAINT_ATTRS)


def test_the_hash_carries_every_constraint_pydantic_knows_except_the_reasoned_exclusions():
    pydantic_knows = set(FieldInfo.metadata_lookup)

    assert set(CONSTRAINT_ATTRS) | set(EXCLUDED) == pydantic_knows, (
        "pydantic's constraint set and ours differ: put each new name in CONSTRAINT_ATTRS "
        f"(and CASES) or in EXCLUDED with a reason: {sorted(pydantic_knows ^ (set(CONSTRAINT_ATTRS) | set(EXCLUDED)))}"
    )
    assert not set(CONSTRAINT_ATTRS) & set(EXCLUDED)


@pytest.mark.parametrize("attr", sorted(CASES))
def test_a_change_in_the_constraint_changes_the_signature_hash(attr):
    first, second, tp = CASES[attr]

    assert _signature_hash(first, tp) != _signature_hash(second, tp)


def test_CONTROL_the_same_constraint_hashes_the_same():
    first, _, tp = CASES["allow_inf_nan"]

    assert _signature_hash(first, tp) == _signature_hash(Field(allow_inf_nan=True), tp)

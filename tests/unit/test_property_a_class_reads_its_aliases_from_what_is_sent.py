"""A generated model whose aliases name other fields is read back by its declared class from what
each send path sends, or the wire's known limit says why not.

For each case (`tests.fixtures.properties.aliases`), the declared class reads what a generated
client (`ServiceClient._encode`) and the wire (`wire_models`) send for the value: EQUAL or REFUSED.
In about two cases in five the declared class is a base the value's class redeclares fields of.
A LOST or OTHER reading is allowed only under H-W: on the wire, what was sent is what the wire sends
choosing among the dumps alone, as it did before the validation-alias form existed
(`tests.fixtures.properties.hierarchy.H_W`). The client path has no limit.
"""

from __future__ import annotations

from typing import Any

import pydantic_core
import pytest

import cliffracer.core.validation as validation
from cliffracer.client import ServiceClient
from tests.fixtures.properties import (
    Finding,
    assert_control_finds,
    assert_only_known_limits,
    cases,
    judge,
    seeds,
)
from tests.fixtures.properties import aliases as A
from tests.fixtures.properties import hierarchy as H

pytestmark = pytest.mark.unit

FIXED_SEED = 7
CASES = 1000
CONTROL_CASES = 300
#: A third of the uncovered findings the CONTROL makes over its cases on main (118 on seed 7; 127
#: and 93 on seeds 20261003 and 424242).
CONTROL_FLOOR = 39

LIMITS = [H.H_W]


def findings(seed: int, count: int) -> list[Finding]:
    client = H._client()
    found = []
    for index in range(count):
        case = A.build(seed, index)
        if case is None:
            continue
        for path in A.PATHS:
            cell = A.cell(path, case, client)
            if cell.outcome in ("LOST", "OTHER"):
                found.append(
                    Finding(
                        seed,
                        index,
                        f"{path} ({case.kind}-declared): {case.declared.__name__} reads "
                        f"{cell.outcome} from {H.canon(cell.sent)}",
                        case.reproduction,
                        (cell, case.value),
                    )
                )
    return found


def test_the_declared_class_reads_what_each_path_sends_or_the_wire_limit_explains_it():
    found = [f for seed in seeds(FIXED_SEED) for f in findings(seed, cases(CASES))]

    assert_only_known_limits(found, LIMITS, check="A, alias shapes")


def _field_names(value: Any, *_: Any, **__: Any) -> Any:
    _field_names.calls += 1  # type: ignore[attr-defined]
    return pydantic_core.to_jsonable_python(value, by_alias=False)


def test_CONTROL_always_the_field_names_breaks_the_property(monkeypatch):
    """Instrument control: both paths are replaced, so no code under test is reached. The same
    generator and classifier, with both paths sending the dump by field name, must find the property
    broken; it shows `test_the_declared_class_reads_what_each_path_sends_or_the_wire_limit_explains_it`
    can fail."""
    _field_names.calls = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(validation, "wire_models", _field_names)
    monkeypatch.setattr(ServiceClient, "_encode", lambda self, value, declared: _field_names(value))

    found = findings(FIXED_SEED, CONTROL_CASES)

    assert _field_names.calls > 0, "the CONTROL's replacement was never called"  # type: ignore[attr-defined]
    assert_control_finds(
        judge(found, LIMITS).uncovered, at_least=CONTROL_FLOOR, control="always the field names"
    )

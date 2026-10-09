"""A generated hierarchy's instance, held in a field that declares any class of its hierarchy, is
read back by that class from what each send path sends.

For each case (`tests.fixtures.properties.hierarchy`) and each model class C of the leaf's
hierarchy, an `Outer(item: C)` holds the leaf instance (`tests.fixtures.properties.nested`); the
wire (`wire_models`) and a generated client (`ServiceClient._encode`) send it. C reads the item back
EQUAL, or the send or the read is REFUSED. No limit applies: a LOST or OTHER reading fails.
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
    seeds,
)
from tests.fixtures.properties import hierarchy as H
from tests.fixtures.properties import nested as N

pytestmark = pytest.mark.unit

FIXED_SEED = 7
CASES = 500
CONTROL_CASES = 300
#: A third of the findings the CONTROL makes over its cases on main (488 on seed 7; 514 and 520 on
#: seeds 20261003 and 424242).
CONTROL_FLOOR = 162


def findings(seed: int, count: int) -> list[Finding]:
    client = H._client()
    found = []
    for index in range(count):
        case = H.build(seed, index)
        if case is None:
            continue
        for declared in case.classes:
            for path in N.PATHS:
                cell = N.cell(path, declared, case.instance, client)
                if cell.outcome in ("LOST", "OTHER"):
                    found.append(
                        Finding(
                            seed,
                            index,
                            f"{path}: Outer(item: {declared.__name__}) reads {cell.outcome} from "
                            f"{H.canon(cell.sent)}",
                            case.reproduction,
                            (cell, case.instance),
                        )
                    )
    return found


def test_the_declared_class_reads_the_nested_value_each_path_sends():
    found = [f for seed in seeds(FIXED_SEED) for f in findings(seed, cases(CASES))]

    assert_only_known_limits(found, [], check="N, a nested model")


def _alias_dump(value: Any, *_: Any, **__: Any) -> Any:
    _alias_dump.calls += 1  # type: ignore[attr-defined]
    return pydantic_core.to_jsonable_python(value, by_alias=True)


def test_CONTROL_always_the_alias_dump_breaks_the_property(monkeypatch):
    """The same generator and classifier, with both paths sending the plain alias dump."""
    _alias_dump.calls = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(validation, "wire_models", _alias_dump)
    monkeypatch.setattr(ServiceClient, "_encode", lambda self, value, declared: _alias_dump(value))

    found = findings(FIXED_SEED, CONTROL_CASES)

    assert _alias_dump.calls > 0, "the CONTROL's replacement was never called"  # type: ignore[attr-defined]
    assert_control_finds(found, at_least=CONTROL_FLOOR, control="always the alias dump")

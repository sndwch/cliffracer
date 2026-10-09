"""The two load refusals in this repository use one limit.

`scripts/check_benchmark_regression.py` refuses to score a benchmark taken on a
busy host, and `cliffracer.testing.host_load` refuses to judge a duration on one. They are
separate mechanisms in separate places, and a second opinion about what "busy"
means would be worse than either -- a run could be too busy to score and quiet
enough to judge, which is a contradiction nobody would look for.

So the constants are asserted equal here rather than imported across, which
would make a test module depend on a release script at import time.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from cliffracer.testing import host_load

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_benchmark_regression.py"


def _script():
    spec = importlib.util.spec_from_file_location("_benchmark_gate_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_headroom_multiple_and_floor_match_the_benchmark_gate():
    """One arithmetic, two call sites."""
    gate = _script()

    assert host_load.LOAD_HEADROOM_MULTIPLE == gate.LOAD_HEADROOM_MULTIPLE, (
        f"the duration refusal uses {host_load.LOAD_HEADROOM_MULTIPLE} and the "
        f"benchmark gate uses {gate.LOAD_HEADROOM_MULTIPLE}; a run could be too "
        "busy for one and quiet enough for the other"
    )
    assert host_load.LOAD_REFERENCE_FLOOR == gate.LOAD_REFERENCE_FLOOR, (
        f"the duration refusal floors at {host_load.LOAD_REFERENCE_FLOOR} and the "
        f"benchmark gate at {gate.LOAD_REFERENCE_FLOOR}"
    )


def test_the_limit_is_the_product_of_the_two():
    """Stated rather than assumed: the limit is what the call sites compare against."""
    assert host_load.LOAD_LIMIT == (
        host_load.LOAD_HEADROOM_MULTIPLE * host_load.LOAD_REFERENCE_FLOOR
    )


def test_CONTROL_the_script_really_defines_both_constants():
    """If the script is refactored to name them differently, this file must know.

    Reading a missing attribute would raise rather than compare, so this pins
    that the names exist before the comparisons above lean on them.
    """
    gate = _script()

    assert isinstance(gate.LOAD_HEADROOM_MULTIPLE, float)
    assert isinstance(gate.LOAD_REFERENCE_FLOOR, float)

"""The kv row of `docs/benchmarks.md` says what the kv benchmark recorded.

`generate_benchmarks_markdown` writes the page from the baseline. The row's last cell
states a guarantee, and the baseline holds the two flags it comes from:
`stress_failure_handled` and `recovery_verified`. A page that says "Verified" for a
baseline that recorded no recovery publishes a guarantee nobody measured, so the cell
is read from the flags, and each flag that is not true is named.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "benchmark_baseline.json"
HISTORY = REPO / "benchmarks_history.json"
VERIFIED = "Graceful failure & recovery: Verified"


def _generator():
    spec = importlib.util.spec_from_file_location(
        "run_benchmarks_for_the_kv_row", REPO / "scripts" / "run_benchmarks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _kv_row(handled: Any, recovered: Any) -> str:
    """The kv row of the page the generator writes for the committed baseline, flags replaced."""
    baseline = copy.deepcopy(json.loads(BASELINE.read_text()))
    kv = baseline["metrics"]["kv"]
    for flag, value in (("stress_failure_handled", handled), ("recovery_verified", recovered)):
        if value is None:
            kv.pop(flag, None)
        else:
            kv[flag] = value
    page = _generator().generate_benchmarks_markdown(baseline, HISTORY)
    rows = [line for line in page.splitlines() if line.startswith("| `cliffracer-kv`")]
    assert len(rows) == 1, rows
    return rows[0]


def test_both_flags_true_says_verified():
    assert _kv_row(True, True).endswith(f"| {VERIFIED} |")


@pytest.mark.parametrize(
    "handled, recovered, says",
    [
        (True, False, ["recovery: Not verified", "Graceful failure: Handled"]),
        (False, True, ["Graceful failure: Not handled", "recovery: Verified"]),
        (False, False, ["Graceful failure: Not handled", "recovery: Not verified"]),
        (True, None, ["recovery: Not verified"]),
        (None, True, ["Graceful failure: Not handled"]),
    ],
    ids=[
        "recovery-failed",
        "failure-not-handled",
        "both-failed",
        "recovery-absent",
        "failure-absent",
    ],
)
def test_a_flag_that_is_not_true_is_named_and_the_page_does_not_say_verified(
    handled, recovered, says
):
    row = _kv_row(handled, recovered)

    assert VERIFIED not in row, row
    for text in says:
        assert text in row, row


def test_CONTROL_the_row_follows_the_flags_not_the_committed_page():
    """Without this, "says Verified" could mean "the row is a constant"."""
    assert _kv_row(True, True) != _kv_row(True, False)

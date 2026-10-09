"""`compare_metrics` forgives a latency rise that is over the threshold only inside its floor.

The floor is 0.5 ms by default and 3.5 ms for a base of 10 ms or more, where event-loop scheduling
variance is a few milliseconds. In the committed baseline no latency lies between 10 ms and the
~23 ms at which the 3.5 ms escalation could matter (its 15% is already larger than the floor), so
the escalation is only reachable with a base of its own. These tests give `compare_metrics` the
numbers directly, each pair a rise the floor forgives and one it does not, over the 15% threshold
in both.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

from check_benchmark_regression import compare_metrics  # noqa: E402

pytestmark = pytest.mark.unit

LATENCY = "rpc.concurrency_10.p50_latency_ms"
THRESHOLD = 0.15


def _passes(base: float, current: float, **kwargs) -> bool:
    (comparison,) = compare_metrics(
        {LATENCY: base}, {LATENCY: current}, threshold=THRESHOLD, **kwargs
    )
    return comparison.passed


@pytest.mark.parametrize(
    ("base", "current", "forgiven", "why"),
    [
        (0.336, 0.736, True, "+0.4 ms on a small base: over 15%, under the 0.5 ms floor"),
        (0.336, 0.936, False, "+0.6 ms: over 15% and over the 0.5 ms floor"),
        (14.0, 17.2, True, "+3.2 ms on a base of 14 ms: over 15%, under the 3.5 ms floor"),
        (14.0, 17.6, False, "+3.6 ms on a base of 14 ms: over 15% and over the 3.5 ms floor"),
        (9.0, 10.8, False, "+1.8 ms on a base just under 10 ms: the floor has not escalated"),
    ],
)
def test_a_rise_over_the_threshold_is_forgiven_only_inside_the_floor(base, current, forgiven, why):
    assert (current - base) / base > THRESHOLD, f"{why}: not over the threshold, test is vacuous"
    assert _passes(base, current) is forgiven, why


def test_a_larger_floor_than_the_escalation_is_not_lowered_to_it():
    """`max(noise_floor_ms, 3.5)`: a floor of 5 ms stays 5 ms on a large base."""
    assert _passes(14.0, 18.5, noise_floor_ms=5.0) is True  # +4.5 ms < 5.0
    assert _passes(14.0, 19.5, noise_floor_ms=5.0) is False  # +5.5 ms


def test_the_floor_argument_is_what_a_small_base_uses():
    assert _passes(0.336, 0.936, noise_floor_ms=1.0) is True  # +0.6 ms < 1.0
    assert _passes(0.336, 1.436, noise_floor_ms=1.0) is False  # +1.1 ms


def test_CONTROL_a_rise_inside_the_threshold_passes_whatever_the_floor():
    """The floor is for what the threshold fails; a rise it does not fail is not its business."""
    assert _passes(1.0, 1.1, noise_floor_ms=0.0) is True  # +10%, +0.1 ms over a zero floor

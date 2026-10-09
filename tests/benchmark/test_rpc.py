"""Continuous benchmark tests for Core RPC throughput and latency."""

from __future__ import annotations

import ast
import inspect
import os
import textwrap

import pytest

from tests.benchmark import benchmarks
from tests.benchmark.benchmarks import DEFAULT_NATS_URL, benchmark_rpc

pytestmark = pytest.mark.benchmark

CONCURRENCY_LEVELS = (10, 100, 1000)

# What separates a collapse from scheduling noise.
#
# The two scaling rules compare measured throughputs. Without a margin the sign
# is decided by whichever way the noise fell: one run flipped this pair at a
# difference of 2.6% -- 6686 against 6863 -- while three other suites shared the
# host, on a branch touching no dispatch or transport code.
#
# A collapse is an order-of-magnitude event, not a few percent. This floor sits
# about nineteen times above the largest difference noise has been observed to
# produce here, and five times above the tenfold drop a real collapse gives, so
# it discriminates between them rather than between two draws of the same
# number. Load is reported in the failure text because a breach that is noise
# and a breach that is real look identical without it.
COLLAPSE_FLOOR = 0.5


def _load() -> str:
    """The one-minute load average, for the failure text."""
    return f"{os.getloadavg()[0]:.2f}"


def assert_rpc_invariants(metrics: dict[str, dict[str, float]]) -> None:
    """The rules a set of RPC measurements has to satisfy.

    Every rule is relative or structural: an ordering between percentiles, a
    ratio between them, or throughput at one concurrency level against
    another. This tier runs in a job that names no hardware class and shares
    its host, where a wall-clock bound measures how busy the box is rather
    than how fast the code is. An absolute number belongs with the benchmark
    job, which pins a machine.

    One function, called by the real benchmark and by the controls below, so a
    control exercises the same expression the benchmark runs. Written out twice
    instead, a change to a rule here would leave the controls asserting the old
    one and still passing.
    """
    for c in CONCURRENCY_LEVELS:
        key = f"concurrency_{c}"
        assert key in metrics, f"Missing benchmark result for {key}"
        data = metrics[key]

        # The one absolute number here, and it is a floor rather than a bound:
        # load can only push latency up, so a busy host cannot breach it. It
        # catches a timer that resolved nothing, which no ratio between
        # percentiles can -- a distribution scaled down together satisfies
        # every relative rule.
        assert data["p50_latency_ms"] > 0.01, f"p50 latency invalid: {data['p50_latency_ms']}"

        # The percentiles are published in order, so they must arrive in order.
        # p95 is reported into the baseline and, until this, was read by nothing.
        assert data["p50_latency_ms"] <= data["p95_latency_ms"], (
            f"p95={data['p95_latency_ms']} below p50={data['p50_latency_ms']}"
        )
        assert data["p95_latency_ms"] <= data["p99_latency_ms"], (
            f"p99={data['p99_latency_ms']} below p95={data['p95_latency_ms']}"
        )
        assert data["p99_latency_ms"] > data["p50_latency_ms"], (
            f"Latency tail collapsed: p99={data['p99_latency_ms']} <= p50={data['p50_latency_ms']}"
        )
        assert data["p99_latency_ms"] < data["p50_latency_ms"] * 50.0, (
            f"Tail latency excessive: p99={data['p99_latency_ms']}, p50={data['p50_latency_ms']}"
        )

    c10 = metrics["concurrency_10"]["throughput_msgs_sec"]
    c100 = metrics["concurrency_100"]["throughput_msgs_sec"]
    c1000 = metrics["concurrency_1000"]["throughput_msgs_sec"]

    assert c100 >= c10 * COLLAPSE_FLOOR, (
        f"Throughput did not scale from concurrency 10 ({c10}) to 100 ({c100}): "
        f"{c100 / c10:.1%} of the concurrency-10 rate, below the {COLLAPSE_FLOOR:.0%} "
        f"floor that separates a collapse from noise. load={_load()}"
    )
    assert c1000 >= c10 * COLLAPSE_FLOOR, (
        f"Throughput collapsed at concurrency 1000 ({c1000}) below 10 ({c10}): "
        f"{c1000 / c10:.1%} of the concurrency-10 rate, below the {COLLAPSE_FLOOR:.0%} "
        f"floor that separates a collapse from noise. load={_load()}"
    )


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_core_rpc_benchmarks():
    """Verify Core RPC scales across concurrency levels with an ordered latency tail."""
    metrics = await benchmark_rpc(DEFAULT_NATS_URL, concurrency_levels=CONCURRENCY_LEVELS)

    assert_rpc_invariants(metrics)


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_CONTROL_a_handler_that_answers_wrongly_fails_the_benchmark(monkeypatch):
    """Break the subject and the harness must refuse to report timings.

    The check that decides is inside `benchmark_rpc`: it compares every reply
    against the value it asked for. Measuring a dispatch path that returns the
    wrong answer, and publishing the throughput anyway, is the failure this
    rules out -- a number is only worth its baseline row if the work it timed
    was correct.
    """

    async def wrong_echo(self, value: int = 42) -> benchmarks.BenchmarkEchoResponse:
        return benchmarks.BenchmarkEchoResponse(status="ok", result=value + 1)

    monkeypatch.setattr(
        benchmarks.BenchmarkRpcService, "echo", benchmarks.rpc(wrong_echo), raising=True
    )

    with pytest.raises(AssertionError):
        await benchmark_rpc(DEFAULT_NATS_URL, concurrency_levels=(10,))


def healthy_metrics() -> dict[str, dict[str, float]]:
    """Measurements that satisfy every rule in `assert_rpc_invariants`.

    Every fixture below is this with one named change. A control proves little
    if its numbers break three rules at once and the assert that happens to
    fire first is the one it names, so the starting point has to be a set of
    measurements the rules accept -- which the positive control below measures
    rather than assumes.
    """
    return {
        f"concurrency_{c}": {
            "throughput_msgs_sec": 9000.0,
            "p50_latency_ms": 1.0,
            "p95_latency_ms": 1.5,
            "p99_latency_ms": 2.0,
        }
        for c in CONCURRENCY_LEVELS
    }


def variant(drop: str = "", **changes: dict[str, float]) -> dict[str, dict[str, float]]:
    """`healthy_metrics()` with one level removed, or named fields replaced.

    Written as a delta from a passing set so a reader can see that a fixture
    differs from healthy measurements only in the fields its own rule reads.
    """
    metrics = healthy_metrics()
    if drop:
        del metrics[drop]
    for key, fields in changes.items():
        metrics[key].update(fields)
    return metrics


# One row per assert in `assert_rpc_invariants`: a fragment of that rule's own
# failure message, and measurements that violate that rule and no other.
#
# The fragment is both what `pytest.raises` matches and the test's id, so the
# name of a control cannot drift from the rule it pins. It drifted before:
# `test_CONTROL_inverted_concurrency_scaling_is_rejected` matched "Throughput
# collapsed", which is the concurrency-1000 rule, so `c100 >= c10` read as
# guarded while nothing exercised it.
BELOW_THE_FLOOR = {"p50_latency_ms": 0.005, "p95_latency_ms": 0.006, "p99_latency_ms": 0.008}

RULES = [
    ("Missing benchmark result", variant(drop="concurrency_1000")),
    # A timer that resolved nothing reports implausibly small latencies. The
    # whole distribution is scaled down together, so the floor is the only
    # rule this breaches -- leaving p99 at 2.0 would breach the tail ratio too.
    ("p50 latency invalid", variant(concurrency_10=BELOW_THE_FLOOR)),
    # p95 is published into the baseline; these two rows are what read it.
    ("below p50", variant(concurrency_10={"p95_latency_ms": 0.5})),
    ("below p95", variant(concurrency_10={"p99_latency_ms": 1.2})),
    # p95 comes down with p99: a tail that collapsed onto p50 and left p95
    # above it would breach the percentile order first and fail for that.
    (
        "Latency tail collapsed",
        variant(concurrency_10={"p95_latency_ms": 1.0, "p99_latency_ms": 1.0}),
    ),
    ("Tail latency excessive", variant(concurrency_10={"p99_latency_ms": 60.0})),
    # The two scaling rules compare different levels against 10, and each row
    # moves only the level its own rule reads.
    ("Throughput did not scale", variant(concurrency_100={"throughput_msgs_sec": 900.0})),
    ("Throughput collapsed", variant(concurrency_1000={"throughput_msgs_sec": 900.0})),
]


@pytest.mark.parametrize(
    ("message", "metrics"), RULES, ids=[message.replace(" ", "_") for message, _ in RULES]
)
def test_CONTROL_each_rule_rejects_the_measurements_it_exists_to_reject(
    message: str, metrics: dict[str, dict[str, float]]
) -> None:
    """Every rule must refuse the numbers it was written to refuse.

    Fed to `assert_rpc_invariants`, the same function the benchmark above
    calls, rather than to a comparison retyped inside this test -- so relaxing
    a rule moves its control with it. `match` pins each control to its own
    rule's message: without it a control passes on any failure, including one
    caused by the fixture drifting out of range of a rule it does not name.
    """
    with pytest.raises(AssertionError, match=message):
        assert_rpc_invariants(metrics)


def test_the_measurements_every_control_starts_from_satisfy_all_the_rules() -> None:
    """The positive control: healthy numbers must pass.

    If this fails, every row above may be passing for a reason that has
    nothing to do with the field it changed.
    """
    assert_rpc_invariants(healthy_metrics())


@pytest.mark.parametrize(
    ("label", "high", "low"),
    [
        # The measurement that actually reddened CI, to the decimal.
        ("the observed flip, 2.6% apart", 6686.0, 6863.1),
        # Either side of the floor, so the boundary itself is pinned.
        ("just above the floor", 4600.0, 9000.0),
    ],
    ids=lambda v: v if isinstance(v, str) else f"{v:g}",
)
def test_CONTROL_a_difference_that_is_noise_is_not_a_collapse(
    label: str, high: float, low: float
) -> None:
    """The rule must accept the numbers that made it fire for no reason.

    A floor is only worth having if it lets through the case it was added for.
    The first row is run 2003's actual pair -- 6686 against 6863, a 2.6%
    difference on a shared host -- which the marginless comparison rejected.
    """
    assert_rpc_invariants(
        variant(
            concurrency_10={"throughput_msgs_sec": low},
            concurrency_100={"throughput_msgs_sec": high},
            concurrency_1000={"throughput_msgs_sec": high},
        )
    )


@pytest.mark.parametrize(
    ("label", "c10", "collapsed"),
    [
        ("a tenfold drop", 9000.0, 900.0),
        # 48.9% of the concurrency-10 rate. The accepting control above sits at
        # 51.1%, which pins the floor from above; this row pins it from below,
        # so the constant is bracketed rather than merely not-1.0. Without it
        # the floor can be walked down to 0.11 with the whole file still green,
        # which is the direction a threshold moves when someone meets a flaky
        # red and edits the number instead of the run.
        ("just below the floor", 9000.0, 4400.0),
    ],
    ids=lambda v: v if isinstance(v, str) else f"{v:g}",
)
def test_CONTROL_a_drop_past_the_floor_is_still_a_collapse(
    label: str, c10: float, collapsed: float
) -> None:
    """And the floor must still refuse the thing the rule exists for.

    Paired with the control above: together they show the rule discriminates
    between the two populations rather than sitting below both or above both.
    """
    with pytest.raises(AssertionError, match="Throughput collapsed"):
        assert_rpc_invariants(
            variant(
                concurrency_10={"throughput_msgs_sec": c10},
                concurrency_1000={"throughput_msgs_sec": collapsed},
            )
        )


def test_the_failure_text_carries_both_numbers_and_the_load() -> None:
    """A breach that is noise and a breach that is real read alike without them.

    The issue this rule comes from was diagnosed only because the two numbers
    were in the message and the host's state could be established separately.
    """
    with pytest.raises(AssertionError) as caught:
        assert_rpc_invariants(
            variant(
                concurrency_10={"throughput_msgs_sec": 9000.0},
                concurrency_1000={"throughput_msgs_sec": 900.0},
            )
        )
    message = str(caught.value)
    assert "900" in message and "9000" in message, message
    assert "load=" in message, message


def rule_messages() -> list[str]:
    """The failure message of every assert in `assert_rpc_invariants`."""
    source = textwrap.dedent(inspect.getsource(assert_rpc_invariants))
    asserts = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assert)]
    assert asserts, "No asserts found; this is reading the wrong function"
    for node in asserts:
        assert node.msg is not None, (
            f"The rule on line {node.lineno} asserts without a message, so no "
            f"control can pin itself to it"
        )
    return [ast.unparse(node.msg) for node in asserts]


def test_every_rule_in_assert_rpc_invariants_has_exactly_one_control() -> None:
    """A rule added without a control is a rule nothing proves can fail.

    Read out of the function's source rather than counted by hand, so a rule
    added later fails this until a row above claims it.
    """
    messages = rule_messages()
    claimed: dict[str, str] = {}
    for fragment, _ in RULES:
        hits = [message for message in messages if fragment in message]
        assert len(hits) == 1, f"{fragment!r} matches {len(hits)} rules, not 1: {hits}"
        assert hits[0] not in claimed, f"{fragment!r} and {claimed[hits[0]]!r} pin the same rule"
        claimed[hits[0]] = fragment

    unwatched = [message for message in messages if message not in claimed]
    assert not unwatched, f"Rules with no control in RULES: {unwatched}"

#!/usr/bin/env python3
"""Compare continuous benchmark runs against baseline and fail on >15% regression.

Usage:
    uv run python scripts/check_benchmark_regression.py --baseline benchmark_baseline.json [--current benchmark_current.json ...] [--threshold 0.15]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# Core key metrics as designated in framework SLAs
# The metrics a threshold breach fails the build on. A number whose subject can
# be deleted to improve it does not belong here: a throughput that rises when
# the component stops doing its work is reported and not gated.
KEY_METRICS = {
    "rpc.concurrency_10.throughput_msgs_sec",
    "rpc.concurrency_10.p50_latency_ms",
    "rpc.concurrency_100.throughput_msgs_sec",
    "rpc.concurrency_100.p50_latency_ms",
    "rpc.concurrency_1000.throughput_msgs_sec",
    "rpc.concurrency_1000.p50_latency_ms",
    "jetstream.throughput_msgs_sec",
    "jetstream.p50_batch_latency_ms",
    "auth.token_validation_ops_sec",
    "auth.modern_hash_ops_sec",
    "kv.bulk_put_ops_sec",
    "kv.bulk_get_ops_sec",
    # The msgpack-over-JSON speedups at 1 KB and 100 KB: a ratio of two timings taken in the same
    # process within seconds, so the two slow down together and the ratio is steadier than either.
    # On the baseline's machine at 1-minute load 1.6 to 1.9, just under the load this gate scores up
    # to, the medians of three runs of the full benchmark read within 5% of the baseline at these two
    # sizes, though one single run of the 1 KB serialize read 36% below it. 1 MB is left out: it is
    # the noisiest, with single runs of its serialize down to 16% below the baseline.
    "serialization.1KB.msgpack_ser_speedup",
    "serialization.1KB.msgpack_deser_speedup",
    "serialization.100KB.msgpack_ser_speedup",
    "serialization.100KB.msgpack_deser_speedup",
}


RUNNER_SPEC_FIELDS = ("cpu_count", "cpu_arch", "total_ram_gb", "os", "python_version")


def runner_spec(runner: dict[str, Any]) -> dict[str, Any]:
    """The hardware-class fields of a runner block.

    A mismatch here refuses to score the run, so it must read only what
    describes the machine. The block also carries the load the run was taken
    under, which differs every time: comparing the whole block would mark every
    run as a different machine and refuse every run.
    """
    return {k: runner[k] for k in RUNNER_SPEC_FIELDS if k in runner}


def machine_differences(
    baseline_data: dict[str, Any], current_data: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    """(field, baseline value, current value) for each machine field that differs.

    `platform` is compared whole and the runner block by `RUNNER_SPEC_FIELDS`.
    A block missing from either side is not compared: an absent reading says
    nothing about the machine.
    """
    differences: list[tuple[str, Any, Any]] = []
    baseline_platform = baseline_data.get("platform", {})
    current_platform = current_data.get("platform", {})
    if baseline_platform and current_platform:
        for key in sorted(set(baseline_platform) | set(current_platform)):
            if baseline_platform.get(key) != current_platform.get(key):
                differences.append(
                    (f"platform.{key}", baseline_platform.get(key), current_platform.get(key))
                )
    baseline_spec = runner_spec(baseline_data.get("environment", {}).get("runner", {}))
    current_spec = runner_spec(current_data.get("environment", {}).get("runner", {}))
    if baseline_spec and current_spec:
        for key in RUNNER_SPEC_FIELDS:
            if baseline_spec.get(key) != current_spec.get(key):
                differences.append(
                    (f"environment.runner.{key}", baseline_spec.get(key), current_spec.get(key))
                )
    return differences


def load_summary(data: dict[str, Any]) -> str:
    """The one-minute load the run was measured under, or that it was not recorded."""
    runner = data.get("environment", {}).get("runner", {})
    load = runner.get("load_average")
    if not isinstance(load, dict) or "1min" not in load:
        return "load not recorded"
    name = runner.get("runner_name")
    where = f" on {name}" if name else ""
    return f"1-min load {load['1min']}{where}"


# WHEN THE GATE REFUSES TO SCORE RATHER THAN SCORING BADLY.
#
# A run taken on a busy host reads as a regression: every metric moves toward
# slower at once, and the two that cross the threshold are the two most
# sensitive to contention rather than the two the code changed. Recording both
# loads and printing a caveat, then failing anyway, leaves the reader to
# overrule the gate by hand -- which is the opposite of what a gate is for.
#
# The multiple is generous on purpose. The baseline's load is a HAND-RECORDED
# midpoint of an observation (`recorded_by` in the baseline says so, and its
# 5-min and 15-min values were never read), so the reference is not a
# measurement this script made. A tight multiple over a hand-entered number
# would refuse quiet runs.
#
# The floor is why a multiple is safe at all: a baseline recorded on an
# unusually idle host would otherwise make the gate hair-trigger, since twice
# almost-nothing is still almost-nothing.
#
# A CONSEQUENCE WORTH KNOWING BEFORE ANYONE "FIXES" THE HAND-ENTERED 1.0: while
# the baseline's recorded load sits at or below the floor, it does not affect
# this limit at all -- the reference is the floor either way. Measuring the host
# properly and writing the true quiet median into the baseline therefore changes
# nothing here, and lowering the floor to match it is what would bite: at a
# median of 0.46 the limit becomes 0.92 and refuses about a fifth of quiet runs.
# The floor is a statement about what load this gate considers ordinary, not an
# estimate of the baseline's conditions.
LOAD_HEADROOM_MULTIPLE = 2.0
LOAD_REFERENCE_FLOOR = 1.0

# A refusal is not a regression and not a pass, and the three call for
# different responses -- read your diff, re-run on a quiet host, ship it. The
# message says which; this makes it readable by something other than a person.
EXIT_NOT_SCORED = 2


def one_minute_load(data: dict[str, Any]) -> float | None:
    """The one-minute load a run recorded, or None if it recorded none."""
    runner = data.get("environment", {}).get("runner", {})
    load = runner.get("load_average")
    if not isinstance(load, dict):
        return None
    value = load.get("1min")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def load_too_high_to_score(
    baseline_data: dict[str, Any],
    datasets: list[dict[str, Any]],
    multiple: float = LOAD_HEADROOM_MULTIPLE,
) -> tuple[float, float] | None:
    """(the offending current load, the limit) when the host was too busy.

    None when every current run is inside the limit, or when a current run
    recorded no load at all -- an absent reading cannot be compared, and it is
    already reported as absent rather than passed over.
    """
    baseline_load = one_minute_load(baseline_data)
    reference = max(baseline_load or 0.0, LOAD_REFERENCE_FLOOR)
    limit = reference * multiple
    for data in datasets:
        current = one_minute_load(data)
        if current is not None and current > limit:
            return current, limit
    return None


def current_load_summary(datasets: list[dict[str, Any]]) -> str:
    """The load of every current run, not only the first.

    `--current` accepts more than one file, and a summary naming one of them
    says nothing about the rest.
    """
    if not datasets:
        return "load not recorded"
    summaries = [load_summary(d) for d in datasets]
    if len(summaries) == 1:
        return summaries[0]
    return "; ".join(f"run {i + 1}: {t}" for i, t in enumerate(summaries))


def incomplete_environment(data: dict[str, Any]) -> str | None:
    """The probe failure recorded by a run, or None if the broker was read.

    `get_environment_context` omits the version, JetStream setting and payload
    limit when its probe raises, and records `probe_failed` in their place
    rather than defaulting them. A comparison against a baseline taken on a
    known server is not meaningful when the current run does not know what it
    measured against, so the gate refuses instead of scoring it.
    """
    nats = data.get("environment", {}).get("nats", {})
    failure = nats.get("probe_failed")
    return str(failure) if failure else None


class MetricComparison(NamedTuple):
    name: str
    baseline: float
    current: float
    delta_pct: float
    threshold_pct: float
    passed: bool
    is_higher_better: bool
    is_key_metric: bool


def flatten_metrics(metrics: dict[str, Any], prefix: str = "") -> dict[str, float]:
    """Recursively flatten metrics dict to dot-separated float values."""
    flat: dict[str, float] = {}
    for key, value in metrics.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten_metrics(value, full_key))
        elif isinstance(value, int | float) and not isinstance(value, bool):
            flat[full_key] = float(value)
    return flat


class UnknownMetricDirection(ValueError):
    """Nothing declares which way this metric is supposed to move."""


HIGHER_IS_BETTER_KEYWORDS = ("throughput", "msgs_sec", "ops_sec", "speedup")
LOWER_IS_BETTER_KEYWORDS = ("latency", "_ms", "_time", "duration", "overhead", "size_bytes")

# Metrics the keyword rules do not decide, or decide wrongly, each with why.
# A keyword reads the name; these read the thing being measured.
EXPLICIT_DIRECTIONS: dict[str, tuple[bool, str]] = {
    "auth.modern_hash_ops_sec": (
        False,
        "a password KDF is deliberately slow. More hashes per second means a "
        "lower work factor, which is the regression, so the keyword rule would "
        "score a weakened KDF as an improvement.",
    ),
    "jetstream.messages_acked": (
        True,
        "a count of messages acknowledged during the run; fewer is lost work.",
    ),
    "jetstream.messages_consumed": (
        True,
        "a count of messages consumed during the run; fewer is lost work.",
    ),
    "kv.items_processed": (
        True,
        "a count of key-value operations completed; fewer is lost work.",
    ),
}

# Values that describe how a run was configured, or what it confirmed, rather
# than how well it performed. A percentage against the baseline says nothing
# useful about these, so they are not compared.
NOT_A_PERFORMANCE_SIGNAL: dict[str, str] = {
    "jetstream.batch_size": (
        "the batch size the run was configured with. It is an input to the "
        "measurement rather than a result of it, and changing it changes what "
        "the other jetstream numbers mean rather than improving or worsening "
        "them."
    ),
}


def metric_direction(metric_name: str) -> bool:
    """Whether a larger number is the better one for this metric.

    Raises rather than guessing. A metric nobody has classified is a question,
    not a latency: defaulting it to lower-is-better is how a counter of
    completed work came to score a halved run as a 50% improvement.
    """
    if metric_name in EXPLICIT_DIRECTIONS:
        return EXPLICIT_DIRECTIONS[metric_name][0]
    if any(kw in metric_name for kw in HIGHER_IS_BETTER_KEYWORDS):
        return True
    if any(kw in metric_name for kw in LOWER_IS_BETTER_KEYWORDS):
        return False
    raise UnknownMetricDirection(
        f"{metric_name!r} matches no direction keyword and is not declared in "
        "EXPLICIT_DIRECTIONS or NOT_A_PERFORMANCE_SIGNAL. Say which way it is "
        "supposed to move, and why."
    )


def undeclared_metrics(names: Iterable[str]) -> list[str]:
    """Return the metrics nothing classifies, so the gate can refuse up front."""
    undeclared = []
    for name in sorted(names):
        if name in NOT_A_PERFORMANCE_SIGNAL:
            continue
        try:
            metric_direction(name)
        except UnknownMetricDirection:
            undeclared.append(name)
    return undeclared


def is_higher_better_metric(metric_name: str) -> bool:
    """Whether a larger number is the better one for this metric."""
    return metric_direction(metric_name)


def compare_metrics(
    baseline_flat: dict[str, float],
    current_flat: dict[str, float],
    threshold: float = 0.05,
    key_metrics_only: bool = False,
    noise_floor_ms: float = 0.5,
) -> list[MetricComparison]:
    """Compare metrics in baseline against current run with calibrated noise tolerance."""
    comparisons: list[MetricComparison] = []
    threshold_pct = threshold * 100.0

    for key, base_val in sorted(baseline_flat.items()):
        if key in NOT_A_PERFORMANCE_SIGNAL:
            continue
        is_key = key in KEY_METRICS
        if key_metrics_only and not is_key:
            continue

        # A key metric the current run lacks is refused by `main` before this
        # runs, naming each one; a missing non-key metric is not compared.
        if key not in current_flat:
            continue

        curr_val = current_flat[key]
        if base_val == 0.0:
            continue

        higher_better = is_higher_better_metric(key)

        # Calculate relative delta percentage
        delta_pct = ((curr_val - base_val) / base_val) * 100.0

        if higher_better:
            # For throughput: negative delta means regression (current < baseline)
            # Regressed if dropped by more than threshold percentage
            passed = delta_pct >= -threshold_pct
        else:
            # For latency: positive delta means regression (current > baseline)
            # In micro-benchmarking, ignore noise under calibrated noise floor.
            # For higher latency operations (e.g. concurrency 1000 where base_val >= 10.0ms),
            # normal event loop scheduling variance is ~1.5-3.2ms, so calibrate noise floor to 3.5ms.
            eff_noise_floor = max(noise_floor_ms, 3.5) if base_val >= 10.0 else noise_floor_ms
            if abs(curr_val - base_val) < eff_noise_floor:
                passed = True
            else:
                passed = delta_pct <= threshold_pct

        comparisons.append(
            MetricComparison(
                name=key,
                baseline=base_val,
                current=curr_val,
                delta_pct=delta_pct,
                threshold_pct=threshold_pct,
                passed=passed,
                is_higher_better=higher_better,
                is_key_metric=is_key,
            )
        )

    return comparisons


def main() -> int:
    parser = argparse.ArgumentParser(description="Check benchmark regression against baseline")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=REPO_ROOT / "benchmark_baseline.json",
        help="Path to baseline JSON file",
    )
    parser.add_argument(
        "--current",
        type=Path,
        nargs="+",
        default=None,
        help="Path to current JSON file(s) (if omitted, baseline is verified against itself)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.15,
        help="Regression threshold (default: 0.15 for 15 percent)",
    )
    parser.add_argument(
        "--noise-floor-ms",
        type=float,
        default=0.5,
        help="Absolute noise floor in ms below which latency differences are ignored (default: 0.5ms)",
    )
    parser.add_argument(
        "--all-metrics",
        action="store_true",
        default=False,
        help="Check all granular metrics rather than filtering to key SLA metrics",
    )
    args = parser.parse_args()

    if not args.baseline.is_file():
        print(f"Error: Baseline file not found: {args.baseline}", file=sys.stderr)
        return 1

    baseline_data = json.loads(args.baseline.read_text(encoding="utf-8"))
    baseline_flat = flatten_metrics(baseline_data.get("metrics", {}))

    # Bound before the branch: the summary names the current runs on both the
    # passing and failing paths, and `--current` is optional.
    current_datasets: list[dict[str, Any]] = []
    if args.current:
        for curr_path in args.current:
            if not curr_path.is_file():
                print(f"Error: Current benchmark file not found: {curr_path}", file=sys.stderr)
                return 1
            current_datasets.append(json.loads(curr_path.read_text(encoding="utf-8")))

        for current_data in current_datasets:
            failures = current_data.get("failures")
            if failures:
                print(
                    f"\nFAILURE: Benchmark run contains execution failures: {failures}",
                    file=sys.stderr,
                )
                return 1

        for current_data in current_datasets:
            probe_failure = incomplete_environment(current_data)
            if probe_failure:
                print(
                    "\nFAILURE: the run could not read the broker, so its environment "
                    f"block is incomplete: {probe_failure}. The numbers were measured "
                    "against a server whose version, JetStream setting and payload "
                    "limit are unknown, and comparing them to a baseline recorded "
                    "against a known one says nothing. Refusing rather than "
                    "reporting a comparison.",
                    file=sys.stderr,
                )
                return 1

        # Before the machine comparison below. Both refuse with the same code,
        # and a busy host is named as one whichever machine it was.
        too_busy = load_too_high_to_score(baseline_data, current_datasets)
        if too_busy is not None:
            current, limit = too_busy
            baseline_load = one_minute_load(baseline_data)
            recorded = "not recorded" if baseline_load is None else f"{baseline_load}"
            print(
                f"\nNOT SCORED: host 1-min load {current} against baseline {recorded}, "
                f"over the limit of {limit:.2f} "
                f"({LOAD_HEADROOM_MULTIPLE:g}x a reference of at least "
                f"{LOAD_REFERENCE_FLOOR:g}).\n"
                "  This is NOT a regression and NOT a pass: the run was taken on a "
                "busy host, where every metric moves toward slower at once and the "
                "ones that cross the threshold are the ones most sensitive to "
                "contention rather than the ones the code changed. Comparing these "
                "numbers to the baseline would report the host as a change in the "
                "code.\n"
                "  Re-run the benchmark on a quiet host. Nothing here says anything "
                "about the diff.",
                file=sys.stderr,
            )
            return EXIT_NOT_SCORED

        # A DIFFERENT MACHINE IS NOT SCORED, for the same reason a busy host is
        # not: the numbers would report the machine as a change in the code. It
        # is not a pass either. A kernel or interpreter update on the benchmark
        # host refuses every run until the baseline is recorded there again,
        # which is the signal to record it.
        for index, current_data in enumerate(current_datasets, start=1):
            differences = machine_differences(baseline_data, current_data)
            if differences:
                which = f"run {index}" if len(current_datasets) > 1 else "the run"
                print(
                    f"\nNOT SCORED: {which} was measured on a different machine from the baseline:",
                    file=sys.stderr,
                )
                for field, was, now in differences:
                    print(f"  - {field}: baseline {was!r}, current {now!r}", file=sys.stderr)
                print(
                    "  This is NOT a regression and NOT a pass: a comparison across "
                    "machines reports the machine as a change in the code. Record the "
                    "baseline on this machine, or run the benchmark on the baseline's.",
                    file=sys.stderr,
                )
                return EXIT_NOT_SCORED

        current_flats = [flatten_metrics(cd.get("metrics", {})) for cd in current_datasets]
        if len(current_flats) == 1:
            current_flat = current_flats[0]
        else:
            all_metric_keys: set[str] = set()
            for cf in current_flats:
                all_metric_keys.update(cf.keys())
            current_flat = {}
            for k in all_metric_keys:
                vals = [cf[k] for cf in current_flats if k in cf]
                if vals:
                    current_flat[k] = float(statistics.median(vals))
    else:
        current_flat = flatten_metrics(baseline_data.get("metrics", {}))

    # Enforce presence of all baseline key SLA metrics
    missing_key_metrics = [
        k for k in sorted(KEY_METRICS) if k in baseline_flat and k not in current_flat
    ]
    if missing_key_metrics:
        print(
            f"\nFAILURE: Missing {len(missing_key_metrics)} key SLA metric(s) in current run:",
            file=sys.stderr,
        )
        for k in missing_key_metrics:
            print(f"  - {k} (required key metric missing from current run)", file=sys.stderr)
        return 1

    # A metric nobody has classified is a failure of the gate, not a value to
    # score lower-is-better by default.
    undeclared = undeclared_metrics(baseline_flat)
    if undeclared:
        print(
            f"\nFAILURE: {len(undeclared)} baseline metric(s) declare no direction:",
            file=sys.stderr,
        )
        for name in undeclared:
            print(f"  - {name}", file=sys.stderr)
        print(
            "\nAdd each to EXPLICIT_DIRECTIONS with the reason, or to "
            "NOT_A_PERFORMANCE_SIGNAL if a percentage against the baseline says "
            "nothing about it.",
            file=sys.stderr,
        )
        return 1

    key_only = not args.all_metrics
    comparisons = compare_metrics(
        baseline_flat,
        current_flat,
        threshold=args.threshold,
        key_metrics_only=key_only,
        noise_floor_ms=args.noise_floor_ms,
    )

    if not comparisons:
        print(
            "Warning: No matching metrics found between baseline and current runs.", file=sys.stderr
        )
        return 1

    # Print comparison table
    print("\n" + "=" * 94)
    scope_str = "KEY SLA METRICS" if key_only else "ALL METRICS"
    print(f"BENCHMARK REGRESSION AUDIT ({scope_str}, Threshold: < {args.threshold * 100:.1f}%)")
    print("=" * 94)

    header = f"{'Metric':<50} | {'Baseline':<10} | {'Current':<10} | {'Delta':<8} | {'Status'}"
    print(header)
    print("-" * 94)

    regressions: list[MetricComparison] = []
    for c in comparisons:
        status_str = "PASS" if c.passed else "FAIL (REGRESSION)"
        delta_sign = "+" if c.delta_pct >= 0 else ""
        delta_str = f"{delta_sign}{c.delta_pct:.1f}%"
        print(
            f"{c.name:<50} | {c.baseline:<10.3f} | {c.current:<10.3f} | {delta_str:<8} | {status_str}"
        )
        if not c.passed:
            regressions.append(c)

    print("=" * 94)

    if regressions:
        print(
            f"\nFAILURE: {len(regressions)} key metric(s) regressed beyond the {args.threshold * 100:.1f}% threshold:"
        )
        baseline_load = load_summary(baseline_data)
        current_load = current_load_summary(current_datasets)
        for r in regressions:
            direction = "drop" if r.is_higher_better else "increase"
            print(
                f"  - {r.name}: baseline={r.baseline:.3f}, current={r.current:.3f} "
                f"({r.delta_pct:.1f}% {direction}) [baseline {baseline_load}; current {current_load}]"
            )
        print(
            f"\n  Measured under: baseline {baseline_load}; current {current_load}. "
            "A run taken on a busy host reads as a regression, so compare these "
            "before reading the numbers above as a change in the code."
        )
        return 1

    print(
        f"\nSUCCESS: All {len(comparisons)} audited metrics within {args.threshold * 100:.1f}% "
        f"performance threshold. Measured under: baseline {load_summary(baseline_data)}; "
        f"current {current_load_summary(current_datasets)}.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

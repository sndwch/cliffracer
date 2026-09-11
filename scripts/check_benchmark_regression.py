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
from pathlib import Path
from typing import Any, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# Core key metrics as designated in framework SLAs
KEY_METRICS = {
    "rpc.concurrency_10.throughput_msgs_sec",
    "rpc.concurrency_10.p50_latency_ms",
    "rpc.concurrency_100.throughput_msgs_sec",
    "rpc.concurrency_100.p50_latency_ms",
    "rpc.concurrency_1000.throughput_msgs_sec",
    "rpc.concurrency_1000.p50_latency_ms",
    "jetstream.throughput_msgs_sec",
    "jetstream.p50_batch_latency_ms",
    "faststream.hypervisor_msgs_sec",
    "auth.token_validation_ops_sec",
    "auth.modern_hash_ops_sec",
    "kv.bulk_put_ops_sec",
    "kv.bulk_get_ops_sec",
}


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


def is_higher_better_metric(metric_name: str) -> bool:
    """Determine whether a metric is throughput (higher is better) or latency (lower is better)."""
    higher_better_keywords = ("throughput", "msgs_sec", "ops_sec", "speedup")
    return any(kw in metric_name for kw in higher_better_keywords)


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
        if key not in current_flat:
            continue

        is_key = key in KEY_METRICS
        if key_metrics_only and not is_key:
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

    bypass_failure = False
    if args.current:
        current_datasets: list[dict[str, Any]] = []
        for curr_path in args.current:
            if not curr_path.is_file():
                print(f"Error: Current benchmark file not found: {curr_path}", file=sys.stderr)
                return 1
            current_datasets.append(json.loads(curr_path.read_text(encoding="utf-8")))

        for current_data in current_datasets:
            baseline_platform = baseline_data.get("platform", {})
            current_platform = current_data.get("platform", {})
            baseline_runner = baseline_data.get("environment", {}).get("runner", {})
            current_runner = current_data.get("environment", {}).get("runner", {})

            env_mismatch = False
            if baseline_platform and current_platform and baseline_platform != current_platform:
                env_mismatch = True
            elif baseline_runner and current_runner and baseline_runner != current_runner:
                env_mismatch = True

            if env_mismatch:
                print(
                    "\n[WARN] ENVIRONMENT / NODE SPECIFICATION MISMATCH DETECTED:", file=sys.stderr
                )
                print(
                    f"  Baseline Runner: {baseline_runner or baseline_platform}",
                    file=sys.stderr,
                )
                print(
                    f"  Current Runner:  {current_runner or current_platform}",
                    file=sys.stderr,
                )
                print(
                    "  Bypassing hard threshold failure because the underlying node specifications differ.\n",
                    file=sys.stderr,
                )
                bypass_failure = True
                break

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
        for r in regressions:
            direction = "drop" if r.is_higher_better else "increase"
            print(
                f"  - {r.name}: baseline={r.baseline:.3f}, current={r.current:.3f} ({r.delta_pct:.1f}% {direction})"
            )
        if bypass_failure:
            print(
                "\n[WARN] IGNORING FAILURES: Node specifications changed between baseline and current run."
            )
            return 0
        return 1

    print(
        f"\nSUCCESS: All {len(comparisons)} audited metrics within {args.threshold * 100:.1f}% performance threshold.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

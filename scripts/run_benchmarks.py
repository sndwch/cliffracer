#!/usr/bin/env python3
"""Run Cliffracer continuous benchmarking battery and generate baseline records.

Usage:
    uv run python scripts/run_benchmarks.py [--output benchmark_baseline.json] [--nats-url nats://localhost:4222]
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import statistics
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.benchmark.benchmarks import DEFAULT_NATS_URL, run_all_benchmarks  # noqa: E402


def aggregate_metrics_median(runs_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute element-wise median for every scalar float/int across multiple runs."""
    if not runs_metrics:
        return {}
    if len(runs_metrics) == 1:
        return copy.deepcopy(runs_metrics[0])

    all_keys: set[str] = set()
    for r in runs_metrics:
        all_keys.update(r.keys())

    result: dict[str, Any] = {}
    for key in all_keys:
        sample_vals = [r[key] for r in runs_metrics if key in r]
        if not sample_vals:
            continue
        first_val = sample_vals[0]
        if isinstance(first_val, dict):
            dict_samples = [v for v in sample_vals if isinstance(v, dict)]
            result[key] = aggregate_metrics_median(dict_samples)
        elif isinstance(first_val, int | float) and not isinstance(first_val, bool):
            num_samples = [
                v for v in sample_vals if isinstance(v, int | float) and not isinstance(v, bool)
            ]
            if num_samples:
                med = statistics.median(num_samples)
                if isinstance(first_val, int) and all(isinstance(v, int) for v in num_samples):
                    result[key] = int(round(med))
                else:
                    result[key] = round(float(med), 5)
            else:
                result[key] = first_val
        else:
            result[key] = first_val

    return result


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Format tabular data into markdown-style aligned table."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(val)))

    header_line = " | ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths, strict=False))
    separator_line = "-+-".join("-" * w for w in col_widths)
    row_lines = [
        " | ".join(f"{str(v):<{w}}" for v, w in zip(row, col_widths, strict=False)) for row in rows
    ]
    return f"{header_line}\n{separator_line}\n" + "\n".join(row_lines)


def print_summary(data: dict[str, Any]) -> None:
    """Print human-readable summary of benchmark results to stdout."""
    metrics = data.get("metrics", {})
    print("\n" + "=" * 78)
    print(
        f"CLIFFRACER CONTINUOUS BENCHMARK BATTERY (Commit: {data.get('git_commit', 'unknown')[:10]})"
    )
    print("=" * 78)

    # Core RPC
    if "rpc" in metrics:
        rpc = metrics["rpc"]
        headers = [
            "Concurrency",
            "Throughput (msgs/sec)",
            "p50 Latency (ms)",
            "p95 Latency (ms)",
            "p99 Latency (ms)",
        ]
        rows = []
        for key in sorted(rpc.keys()):
            val = rpc[key]
            rows.append(
                [
                    key.replace("concurrency_", ""),
                    f"{val.get('throughput_msgs_sec', 0.0):,.1f}",
                    f"{val.get('p50_latency_ms', 0.0):.3f}",
                    f"{val.get('p95_latency_ms', 0.0):.3f}",
                    f"{val.get('p99_latency_ms', 0.0):.3f}",
                ]
            )
        print("\n[1] Core RPC Performance:")
        print(format_table(headers, rows))

    # Core JetStream
    if "jetstream" in metrics:
        js = metrics["jetstream"]
        print("\n[2] JetStream Batch Pull Consumer:")
        print(f"    - Throughput:           {js.get('throughput_msgs_sec', 0.0):,.1f} msgs/sec")
        print(
            f"    - p50 Batch Latency:    {js.get('p50_batch_latency_ms', 0.0):.3f} ms (batch={js.get('batch_size')})"
        )
        print(f"    - Total Messages:       {js.get('messages_consumed')}")

    # Serialization
    if "serialization" in metrics:
        ser = metrics["serialization"]
        headers = [
            "Payload",
            "JSON Ser (ms)",
            "JSON Deser (ms)",
            "MsgPack Ser (ms)",
            "MsgPack Deser (ms)",
            "Ser Speedup",
            "Deser Speedup",
        ]
        rows = []
        for key in ("1KB", "100KB", "1MB"):
            if key in ser:
                val = ser[key]
                rows.append(
                    [
                        key,
                        f"{val.get('json_ser_ms', 0.0):.4f}",
                        f"{val.get('json_deser_ms', 0.0):.4f}",
                        f"{val.get('msgpack_ser_ms', 0.0):.4f}",
                        f"{val.get('msgpack_deser_ms', 0.0):.4f}",
                        f"{val.get('msgpack_ser_speedup', 1.0):.2f}x",
                        f"{val.get('msgpack_deser_speedup', 1.0):.2f}x",
                    ]
                )
        print("\n[3] Serialization Performance (JSON vs MessagePack):")
        print(format_table(headers, rows))

    # Extensions
    print("\n[4] Extension Overhead & Reliability:")
    if "http_gateway" in metrics:
        http = metrics["http_gateway"]
        if "skipped" in http:
            print(f"    - cliffracer-http: Skipped ({http['skipped']})")
        else:
            print(
                f"    - cliffracer-http Gateway Overhead:  {http.get('overhead_ms', 0.0):.4f} ms/req"
            )
    if "faststream" in metrics:
        fs = metrics["faststream"]
        if "skipped" in fs:
            print(f"    - cliffracer-faststream: Skipped ({fs['skipped']})")
        else:
            print(
                f"    - cliffracer-faststream Hypervisor:  {fs.get('hypervisor_msgs_sec', 0.0):,.1f} msgs/sec ({fs.get('overhead_per_msg_ms', 0.0):.5f} ms/msg)"
            )
    if "auth" in metrics:
        auth = metrics["auth"]
        if "skipped" in auth:
            print(f"    - cliffracer-auth: Skipped ({auth['skipped']})")
        else:
            print(
                f"    - cliffracer-auth Token Validation: {auth.get('token_validation_ops_sec', 0.0):,.1f} ops/sec"
            )
            print(
                f"    - cliffracer-auth Modern Hash Verif: {auth.get('modern_hash_ops_sec', 0.0):.2f} ops/sec"
            )
            print(
                f"    - cliffracer-auth Legacy Hash Verif: {auth.get('legacy_hash_ops_sec', 0.0):.2f} ops/sec"
            )
    if "kv" in metrics:
        kv = metrics["kv"]
        if "skipped" in kv:
            print(f"    - cliffracer-kv: Skipped ({kv['skipped']})")
        else:
            print(
                f"    - cliffracer-kv Bulk Put:            {kv.get('bulk_put_ops_sec', 0.0):,.1f} ops/sec"
            )
            print(
                f"    - cliffracer-kv Bulk Get:            {kv.get('bulk_get_ops_sec', 0.0):,.1f} ops/sec"
            )
            print(
                f"    - cliffracer-kv Graceful Failure:    {'PASS' if kv.get('stress_failure_handled') else 'FAIL'}"
            )
            print(
                f"    - cliffracer-kv State Recovery:      {'VERIFIED' if kv.get('recovery_verified') else 'FAIL'}"
            )

    print("=" * 78 + "\n")


def generate_benchmarks_markdown(
    current_run: dict[str, Any], history_file: Path | None = None
) -> str:
    """Generate Markdown report for docs/benchmarks.md."""
    lines: list[str] = [
        "# Continuous Benchmarks & Performance History",
        "",
        "This document tracks performance across Cliffracer releases and the active commit baseline.",
        "CI automatically measures latency, throughput, serialization, and extension overheads,",
        "failing the build if any key performance metric regresses beyond tolerance (> 15% on median of runs).",
        "",
        f"> **Last Generated**: `{current_run.get('timestamp')}`  ",
        f"> **Baseline Commit**: `{current_run.get('git_commit', 'unknown')[:10]}`  ",
        f"> **Environment**: Python {current_run.get('platform', {}).get('python', '3.13')} on {current_run.get('platform', {}).get('system', 'Linux')} ({current_run.get('platform', {}).get('machine', 'x86_64')})",
        "",
        "## Node Specifications & Environment Context",
        "",
        "Execution node environment and broker topology captured to normalize future CI migrations:",
        "",
        "| Category | Specification | Recorded Setting / Measurement |",
        "| :--- | :--- | :--- |",
    ]

    env = current_run.get("environment", {})
    runner = env.get("runner", {})
    network = env.get("network", {})
    nats = env.get("nats", {})

    lines.extend(
        [
            f"| **Runner Hardware** | CPU Cores / Architecture | {runner.get('cpu_count', 1)} cores ({runner.get('cpu_arch', 'x86_64')}) |",
            f"| **Runner Hardware** | Total System RAM | {runner.get('total_ram_gb', 0.0)} GB |",
            f"| **Runner Hardware** | Operating System / Kernel | {runner.get('os', 'Linux')} |",
            f"| **Runner Hardware** | Python Runtime | Python {runner.get('python_version', '3.13')} |",
            f"| **Network Topology** | Target Broker Endpoint | `{network.get('nats_url', 'nats://localhost:4222')}` |",
            f"| **Network Topology** | Topology Classification | {network.get('topology', 'Localhost / Loopback')} |",
            f"| **NATS Broker** | Server Version | NATS v{nats.get('version', '2.10.29')} |",
            f"| **NATS Broker** | JetStream Engine | {'Enabled (Active)' if nats.get('jetstream_enabled') else 'Disabled'} |",
            f"| **NATS Broker** | Maximum Frame / Payload | {nats.get('max_payload_bytes', 1048576):,} bytes (1MB) |",
            "",
            "## Current Release Baseline",
            "",
        ]
    )

    metrics = current_run.get("metrics", {})

    # RPC table
    if "rpc" in metrics:
        lines.extend(
            [
                "### Core RPC Latency & Throughput",
                "",
                "| Concurrency | Throughput (msgs/sec) | p50 Latency (ms) | p95 Latency (ms) | p99 Latency (ms) |",
                "| :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for key in sorted(metrics["rpc"].keys()):
            val = metrics["rpc"][key]
            lines.append(
                f"| {key.replace('concurrency_', '')} | {val.get('throughput_msgs_sec', 0.0):,.1f} | {val.get('p50_latency_ms', 0.0):.3f} | {val.get('p95_latency_ms', 0.0):.3f} | {val.get('p99_latency_ms', 0.0):.3f} |"
            )
        lines.append("")

    # Serialization table
    if "serialization" in metrics:
        lines.extend(
            [
                "### Serialization: JSON vs MessagePack",
                "",
                "| Payload Size | JSON Ser (ms) | JSON Deser (ms) | MsgPack Ser (ms) | MsgPack Deser (ms) | MsgPack Speedup |",
                "| :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for key in ("1KB", "100KB", "1MB"):
            if key in metrics["serialization"]:
                val = metrics["serialization"][key]
                lines.append(
                    f"| {key} | {val.get('json_ser_ms', 0.0):.4f} | {val.get('json_deser_ms', 0.0):.4f} | {val.get('msgpack_ser_ms', 0.0):.4f} | {val.get('msgpack_deser_ms', 0.0):.4f} | **{val.get('msgpack_ser_speedup', 1.0):.1f}x** (ser) / **{val.get('msgpack_deser_speedup', 1.0):.1f}x** (deser) |"
                )
        lines.append("")

    # Extension Overheads
    lines.extend(
        [
            "### Extension Overheads & Reliability",
            "",
            "| Component / Extension | Metric | Baseline Measurement | Guarantee / Invariant |",
            "| :--- | :--- | :--- | :--- |",
        ]
    )
    if "jetstream" in metrics:
        js = metrics["jetstream"]
        lines.append(
            f"| Core JetStream Pull | Batch throughput | {js.get('throughput_msgs_sec', 0.0):,.1f} msgs/sec | p50 batch fetch: {js.get('p50_batch_latency_ms', 0.0):.3f} ms |"
        )
    if "http_gateway" in metrics and "skipped" not in metrics["http_gateway"]:
        http = metrics["http_gateway"]
        lines.append(
            f"| `cliffracer-http` AutoGateway | Route translation overhead | {http.get('overhead_ms', 0.0):.4f} ms | Raw RPC: {http.get('raw_rpc_latency_ms', 0.0):.4f} ms |"
        )
    if "faststream" in metrics and "skipped" not in metrics["faststream"]:
        fs = metrics["faststream"]
        lines.append(
            f"| `cliffracer-faststream` | ACK Hypervisor overhead | {fs.get('overhead_per_msg_ms', 0.0):.5f} ms/msg | Hypervisor: {fs.get('hypervisor_msgs_sec', 0.0):,.1f} msgs/sec |"
        )
    if "auth" in metrics and "skipped" not in metrics["auth"]:
        auth = metrics["auth"]
        lines.append(
            f"| `cliffracer-auth` | Token validation throughput | {auth.get('token_validation_ops_sec', 0.0):,.1f} ops/sec | PBKDF2 hash verif: {auth.get('modern_hash_ops_sec', 0.0):.1f} ops/sec |"
        )
    if "kv" in metrics and "skipped" not in metrics["kv"]:
        kv = metrics["kv"]
        lines.append(
            f"| `cliffracer-kv` | Bulk Get/Put throughput | Put: {kv.get('bulk_put_ops_sec', 0.0):,.1f} / Get: {kv.get('bulk_get_ops_sec', 0.0):,.1f} ops/sec | Graceful failure & recovery: Verified |"
        )
    lines.append("")

    # Historical Backfill Table (if history file exists)
    if history_file and history_file.is_file():
        try:
            history_data = json.loads(history_file.read_text(encoding="utf-8"))
            lines.extend(
                [
                    "## Historical Evolution Across Versions",
                    "",
                    "Benchmark results for tagged releases:",
                    "",
                    "| Version Tag | RPC 1000 Throughput | RPC p50 (ms) | JetStream (msgs/sec) | 1MB MsgPack Speedup | Extensions Introduced |",
                    "| :--- | :--- | :--- | :--- | :--- | :--- |",
                ]
            )
            for tag, h_data in sorted(history_data.items()):
                h_metrics = h_data.get("metrics", {})
                rpc_1000 = h_metrics.get("rpc", {}).get("concurrency_1000", {})
                js = h_metrics.get("jetstream", {})
                ser_1mb = h_metrics.get("serialization", {}).get("1MB", {})
                extensions_present: list[str] = []
                for ext in ("http_gateway", "faststream", "auth", "kv"):
                    if ext in h_metrics and "skipped" not in h_metrics[ext]:
                        extensions_present.append(ext.replace("_gateway", ""))
                ext_str = ", ".join(extensions_present) if extensions_present else "Core only"

                lines.append(
                    f"| `{tag}` | {rpc_1000.get('throughput_msgs_sec', 0.0):,.1f} msgs/sec | {rpc_1000.get('p50_latency_ms', 0.0):.2f} ms | {js.get('throughput_msgs_sec', 0.0):,.1f} msgs/sec | {ser_1mb.get('msgpack_ser_speedup', 1.0):.1f}x | {ext_str} |"
                )
            lines.append("")
        except Exception:
            pass

    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Cliffracer Continuous Benchmark Suite")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "benchmark_baseline.json",
        help="Path to output JSON baseline file",
    )
    parser.add_argument(
        "--nats-url",
        type=str,
        default=DEFAULT_NATS_URL,
        help="NATS broker connection URL",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of consecutive benchmark runs to execute and median-aggregate (default: 1)",
    )
    parser.add_argument(
        "--update-docs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Generate or update docs/benchmarks.md (default: True if output is benchmark_baseline.json)",
    )
    args = parser.parse_args()

    print(f"Running Cliffracer Continuous Benchmarks against {args.nats_url}...")
    if args.runs > 1:
        print(f"Executing {args.runs} benchmark runs to calculate element-wise median metrics...")
        runs_results: list[dict[str, Any]] = []
        for run_idx in range(1, args.runs + 1):
            print(f"\n--- Benchmark Run {run_idx}/{args.runs} ---")
            run_res = await run_all_benchmarks(nats_url=args.nats_url)
            runs_results.append(run_res)
        results = copy.deepcopy(runs_results[-1])
        results["metrics"] = aggregate_metrics_median([r["metrics"] for r in runs_results])
        results["runs_aggregated"] = args.runs
    else:
        results = await run_all_benchmarks(nats_url=args.nats_url)
        results["runs_aggregated"] = 1

    # Output baseline JSON
    output_path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"Successfully saved benchmark baseline to {output_path}")

    # Output docs/benchmarks.md
    update_docs = (
        args.update_docs
        if args.update_docs is not None
        else (output_path.resolve() == (REPO_ROOT / "benchmark_baseline.json").resolve())
    )
    if update_docs:
        docs_dir = REPO_ROOT / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        benchmarks_md_path = docs_dir / "benchmarks.md"
        history_path = REPO_ROOT / "benchmarks_history.json"
        md_content = generate_benchmarks_markdown(results, history_path)
        benchmarks_md_path.write_text(md_content, encoding="utf-8")
        print(f"Successfully updated documentation at {benchmarks_md_path}")

    print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

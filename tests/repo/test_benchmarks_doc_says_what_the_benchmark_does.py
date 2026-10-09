"""`docs/benchmarks.md` says what the benchmark pipeline measures and how it is scored.

The page is generated from `benchmark_baseline.json` and `benchmarks_history.json`
by `scripts/run_benchmarks.py`, so every number on it is a recorded measurement.
What a reader cannot get from the data is what the page says ABOUT the numbers,
and each of those statements is read here from the thing that decides it:

* the page is exactly what the generator writes for the committed data, so a
  hand edit or a baseline recorded without regenerating shows;
* the gate fails beyond 15%, on the median of the runs CI takes, and passes a
  drop of exactly 15%;
* an RPC figure is a call through `CliffracerService`, and a serialization
  figure goes through the `cliffracer.core.validation` wrappers;
* the speedup column is the JSON time over the MessagePack time;
* the baseline behind the extension row's "Verified" recorded a recovery.
  The generator writes that word without reading the flag, so this reads the
  flag itself and cannot tell the word from the data if the two part.

The page also says one benchmark job runs at a time on a host; that is held by
`test_benchmark_job_runs_alone.py`, which reads the workflow's concurrency group.
It says two measuring together move the figures by more than the gate allows. That
is a measurement of contention on the CI host, not something a unit test can take,
and it is not pinned.
"""

from __future__ import annotations

import ast
import copy
import difflib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "benchmarks.md"
BASELINE = REPO / "benchmark_baseline.json"
HISTORY = REPO / "benchmarks_history.json"
CHECKER = REPO / "scripts" / "check_benchmark_regression.py"
BENCHMARKS = REPO / "tests" / "benchmark" / "benchmarks.py"
WORKFLOW = REPO / ".gitea" / "workflows" / "ci.yml"

sys.path.insert(0, str(REPO / "scripts"))
from check_benchmark_regression import KEY_METRICS, compare_metrics  # noqa: E402


def _load_run_benchmarks():
    spec = importlib.util.spec_from_file_location(
        "run_benchmarks_for_the_benchmarks_doc", REPO / "scripts" / "run_benchmarks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_benchmarks = _load_run_benchmarks()

RPC_THROUGHPUT = "rpc.concurrency_1000.throughput_msgs_sec"
RPC_LATENCY = "rpc.concurrency_1000.p50_latency_ms"


def _baseline() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def _benchmark_step_commands() -> str:
    """The `run:` text of every step in the benchmark job, joined."""
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["benchmark"]
    return "\n".join(step["run"] for step in job["steps"] if "run" in step)


# --- the page is the generator's output for the committed data --------------


def test_the_page_is_what_the_generator_writes_for_the_committed_data():
    page = run_benchmarks.generate_benchmarks_markdown(_baseline(), HISTORY)
    committed = DOC.read_text()

    # pytest's own diff of two long strings is cut short without -vv, before the line that
    # differs, so the message carries one.
    diff = difflib.unified_diff(
        committed.splitlines(),
        page.splitlines(),
        "docs/benchmarks.md (committed)",
        "docs/benchmarks.md (generated)",
        lineterm="",
    )
    assert committed == page, (
        "docs/benchmarks.md differs from generate_benchmarks_markdown over "
        "benchmark_baseline.json and benchmarks_history.json; regenerate it with "
        "scripts/run_benchmarks.py:\n" + "\n".join(list(diff)[:60])
    )


def test_CONTROL_a_different_baseline_makes_a_different_page():
    """Without this, "the page matches" could mean "the page ignores its input"."""
    changed = _baseline()
    changed["metrics"]["rpc"]["concurrency_1000"]["throughput_msgs_sec"] += 1000.0

    page = run_benchmarks.generate_benchmarks_markdown(changed, HISTORY)

    assert page != DOC.read_text()


# --- the 15% gate on the median of the runs ---------------------------------


@pytest.mark.gitea_checkout
def test_the_page_states_the_threshold_and_the_median_ci_uses():
    commands = _benchmark_step_commands()
    threshold = re.search(r"check_benchmark_regression\.py[^\n]*--threshold\s+(\S+)", commands)
    runs = re.search(r"run_benchmarks\.py[^\n]*--runs\s+(\d+)", commands)
    assert threshold is not None, commands
    assert runs is not None, commands

    assert round(float(threshold.group(1)) * 100) == 15
    assert int(runs.group(1)) > 1, "a median over one run is the run"
    assert "> 15% on median of runs" in DOC.read_text()


@pytest.mark.parametrize(
    ("metric", "base", "current", "passes"),
    [
        (RPC_THROUGHPUT, 100.0, 85.0, True),
        (RPC_THROUGHPUT, 100.0, 84.0, False),
        (RPC_LATENCY, 100.0, 115.0, True),
        (RPC_LATENCY, 100.0, 116.0, False),
    ],
    ids=["throughput-at-15", "throughput-past-15", "latency-at-15", "latency-past-15"],
)
def test_a_key_metric_fails_only_beyond_15_percent(metric, base, current, passes):
    assert metric in KEY_METRICS
    (comparison,) = compare_metrics({metric: base}, {metric: current}, threshold=0.15)
    assert comparison.passed is passes


def _write_runs(tmp_path: Path, factors: tuple[float, ...]) -> list[str]:
    """`--current` and one run per factor, each scaling the 1000-way RPC throughput.

    `--current` takes several paths after ONE flag; repeating the flag keeps only
    the last file, which would make every outlier case below read a single run.
    """
    paths: list[str] = ["--current"]
    for index, factor in enumerate(factors):
        run = copy.deepcopy(_baseline())
        run["metrics"]["rpc"]["concurrency_1000"]["throughput_msgs_sec"] *= factor
        path = tmp_path / f"run{index}.json"
        path.write_text(json.dumps(run))
        paths.append(str(path))
    return paths


def _gate(currents: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--baseline",
            str(BASELINE),
            *currents,
            "--threshold",
            "0.15",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def test_one_outlying_run_does_not_fail_the_gate(tmp_path):
    """The median of (0.01, 1.0, 1.0) is the baseline; the mean is a 66% drop."""
    result = _gate(_write_runs(tmp_path, (0.01, 1.0, 1.0)))

    assert result.returncode == 0, result.stdout + result.stderr


def test_CONTROL_a_regression_in_most_runs_fails_the_gate(tmp_path):
    result = _gate(_write_runs(tmp_path, (0.5, 0.5, 1.0)))

    assert result.returncode == 1, result.stdout + result.stderr


def test_the_aggregate_takes_the_median_of_each_metric():
    runs = []
    for value in (100.0, 100.0, 1.0):
        run = copy.deepcopy(_baseline())
        run["metrics"]["rpc"]["concurrency_1000"]["throughput_msgs_sec"] = value
        runs.append(run)

    aggregate = run_benchmarks.aggregate_runs(runs)

    assert aggregate["metrics"]["rpc"]["concurrency_1000"]["throughput_msgs_sec"] == 100.0


# --- what an RPC figure and a serialization figure go through ---------------


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{BENCHMARKS} defines no {name}")


def _calls(node: ast.AST) -> list[str]:
    """Each call in `node`, spelled as the dotted name it is made through."""
    spelled = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            target = child.func
            parts = []
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
            spelled.append(".".join(reversed(parts)))
    return spelled


def test_an_rpc_figure_is_a_call_through_a_cliffracer_service():
    tree = ast.parse(BENCHMARKS.read_text())
    service = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "BenchmarkRpcService"
    )
    assert [base.id for base in service.bases if isinstance(base, ast.Name)] == [
        "CliffracerService"
    ]

    calls = _calls(_function(tree, "benchmark_rpc"))

    assert "BenchmarkRpcService" in calls
    assert "svc.call_rpc" in calls
    raw = [call for call in calls if call.endswith((".request", "nats.connect"))]
    assert not raw, f"benchmark_rpc reaches the transport directly through {raw}"


def test_CONTROL_the_call_reader_sees_a_raw_request():
    """The reader above returns names; a raw `nc.request` has to show up in them."""
    tree = ast.parse("async def f(nc):\n    await nc.request('s', b'')\n")

    assert "nc.request" in _calls(tree)


def test_a_serialization_figure_goes_through_the_validation_wrappers(monkeypatch):
    """Each wrapper is the one `validation` defines, and the TIMED loops call it.

    One warmup call per wrapper happens either way, so a bare "was it called"
    would stay green with the timed loop calling a codec directly. The count has
    to exceed the warmup by at least the iterations of one trial.
    """
    from cliffracer.core import validation
    from tests.benchmark import benchmarks

    iterations = 5
    calls: dict[str, int] = {}

    def spy(name: str):
        real = getattr(validation, name)
        assert getattr(benchmarks, name) is real, f"benchmarks.{name} is not validation.{name}"

        def wrapped(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return real(*args, **kwargs)

        return wrapped

    wrappers = ("serialize_payload", "deserialize_payload", "pack_msgpack", "unpack_msgpack")
    for name in wrappers:
        monkeypatch.setattr(benchmarks, name, spy(name))

    benchmarks.benchmark_serialization(payload_specs=(("1KB", 1024, iterations),))

    for name in wrappers:
        assert calls.get(name, 0) >= 1 + iterations, f"{name} was called {calls.get(name, 0)} times"


def test_the_speedup_column_is_the_json_time_over_the_msgpack_time():
    from tests.benchmark import benchmarks

    row = benchmarks.benchmark_serialization(payload_specs=(("1MB", 1024 * 1024, 3),))["1MB"]

    assert row["msgpack_ser_speedup"] == pytest.approx(
        row["json_ser_ms"] / row["msgpack_ser_ms"], rel=0.05
    )
    assert row["msgpack_deser_speedup"] == pytest.approx(
        row["json_deser_ms"] / row["msgpack_deser_ms"], rel=0.05
    )


# --- the extension row's guarantee ------------------------------------------


def test_the_baseline_behind_the_kv_rows_verified_recorded_a_recovery():
    kv = _baseline()["metrics"]["kv"]

    assert "Graceful failure & recovery: Verified" in DOC.read_text()
    assert kv["stress_failure_handled"] is True
    assert kv["recovery_verified"] is True

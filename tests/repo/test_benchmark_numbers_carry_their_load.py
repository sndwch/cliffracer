"""Verify a benchmark number travels with the load it was measured under.

A run taken while the host is busy reads as a regression: two benchmark jobs
together move rpc p50 by up to 26%, against a gate that fails at 15%. Whoever
reads a red run in the morning has to be able to tell that apart from a change
in the code, and re-deriving it after the fact is not possible once the run has
gone.

The load also decides whether the gate scores at all. A mismatch between the
baseline's runner block and the current one refuses to score the run, so the
comparison reads only the fields that describe the machine. Comparing the whole
block would mark every run as a different machine, because the load differs
every time -- refusing every run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_benchmark_regression.py"
BASELINE = REPO / "benchmark_baseline.json"

sys.path.insert(0, str(REPO / "scripts"))
from check_benchmark_regression import (  # noqa: E402
    EXIT_NOT_SCORED,
    LOAD_HEADROOM_MULTIPLE,
    LOAD_REFERENCE_FLOOR,
    RUNNER_SPEC_FIELDS,
    load_summary,
    load_too_high_to_score,
    one_minute_load,
    runner_spec,
)


def _run(current: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--baseline",
            str(BASELINE),
            "--current",
            str(current),
            "--threshold",
            "0.15",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def _regressed(tmp_path: Path, **runner_extra) -> Path:
    """A current run with one metric halved, and whatever runner fields are given."""
    data = json.loads(BASELINE.read_text())
    data["metrics"]["rpc"]["concurrency_10"]["throughput_msgs_sec"] *= 0.5
    runner = data.setdefault("environment", {}).setdefault("runner", {})
    runner.pop("load_average", None)
    runner.pop("runner_name", None)
    runner.update(runner_extra)
    out = tmp_path / "current.json"
    out.write_text(json.dumps(data))
    return out


def test_the_recorded_block_carries_the_load_the_host_reports(monkeypatch, no_broker_monitor):
    """The code that writes the block, not a file that already contains one.

    The committed baseline's reading is hand-entered, so a test that reads it
    is green whatever `get_environment_context` does. This drives the host's
    reading and requires the produced block to carry it.
    """
    import os as os_module

    from tests.benchmark.benchmarks import get_environment_context

    monkeypatch.setattr(os_module, "getloadavg", lambda: (1.5, 2.5, 3.5))
    monkeypatch.setenv("RUNNER_NAME", "tim-probe")

    runner = get_environment_context()["runner"]
    assert runner["load_average"] == {"1min": 1.5, "5min": 2.5, "15min": 3.5}
    assert runner["runner_name"] == "tim-probe"


def test_a_host_that_cannot_report_load_records_the_absence(monkeypatch, no_broker_monitor):
    """The key is present and null, so a reader can tell "not measured" from
    "nobody wrote this field"."""
    import os as os_module

    from tests.benchmark.benchmarks import get_environment_context

    def _no_loadavg():
        raise OSError("no load average on this platform")

    monkeypatch.setattr(os_module, "getloadavg", _no_loadavg)
    monkeypatch.delenv("RUNNER_NAME", raising=False)
    monkeypatch.delenv("GITEA_RUNNER_NAME", raising=False)

    runner = get_environment_context()["runner"]
    assert "load_average" in runner
    assert runner["load_average"] is None
    assert "runner_name" not in runner


def test_the_committed_baseline_records_the_load_it_was_measured_under():
    runner = json.loads(BASELINE.read_text())["environment"]["runner"]
    load = runner.get("load_average")
    assert isinstance(load, dict) and load.get("1min") is not None, (
        "the baseline does not say how busy the host was, so nothing compared "
        "against it can be read for a hardware-class caveat"
    )


def test_a_differing_load_does_not_turn_the_gate_off(tmp_path: Path):
    """The load differs every run. It must not read as a different machine.

    This is the check that makes recording the load safe: comparing the whole
    runner block would refuse every run that carries one.

    The load here sits INSIDE the refusal limit deliberately. Raising it past
    the limit turns this into a test that the gate refuses to score, which is a
    different property with its own tests below, and this one would then pass
    for the wrong reason.
    """
    current = _regressed(
        tmp_path,
        load_average={"1min": 1.5, "5min": 1.4, "15min": 1.2},
        runner_name="tim-3",
    )
    result = _run(current)
    assert result.returncode == 1, (
        "a halved metric passed while the runner block differed only in load:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "different machine" not in result.stderr, result.stderr


def test_a_failure_says_what_the_load_was(tmp_path: Path):
    """Inside the refusal limit, so this reads a scored failure's text."""
    current = _regressed(
        tmp_path, load_average={"1min": 1.5, "5min": 1.4, "15min": 1.2}, runner_name="tim-3"
    )
    result = _run(current)
    assert "1-min load 1.5" in result.stdout, result.stdout
    assert "tim-3" in result.stdout, result.stdout
    assert "Measured under:" in result.stdout, result.stdout


def test_a_current_run_without_a_load_reading_is_reported(tmp_path: Path):
    """Absence is said out loud rather than passed over.

    A blank where the load should be is how a number from a busy host gets
    read as a clean one.
    """
    current = _regressed(tmp_path)
    result = _run(current)
    assert "load not recorded" in result.stdout, result.stdout


def test_the_spec_fields_are_what_describes_the_machine():
    """Named here, not derived from the constant under test.

    Taking the expectation from RUNNER_SPEC_FIELDS makes emptying that tuple
    invisible -- and an empty tuple means every runner block compares equal, so
    the refusal never fires and a genuinely different machine is compared as if
    it were the same one.
    """
    block = {
        "cpu_count": 20,
        "cpu_arch": "x86_64",
        "total_ram_gb": 125.47,
        "os": "Linux 7.0.0",
        "python_version": "3.13.2",
        "load_average": {"1min": 9.9},
        "runner_name": "tim-9",
    }
    assert runner_spec(block) == {
        "cpu_count": 20,
        "cpu_arch": "x86_64",
        "total_ram_gb": 125.47,
        "os": "Linux 7.0.0",
        "python_version": "3.13.2",
    }
    assert set(RUNNER_SPEC_FIELDS) == {
        "cpu_count",
        "cpu_arch",
        "total_ram_gb",
        "os",
        "python_version",
    }


def test_a_different_machine_is_not_scored(tmp_path: Path):
    """The refusal must still fire on the thing it is for.

    Narrowing the comparison to the hardware fields is only safe if those
    fields still trigger it; a comparison that never fires is the same as no
    comparison at all.
    """
    current = _regressed(tmp_path, load_average={"1min": 1.0, "5min": 1.0, "15min": 1.0})
    data = json.loads(current.read_text())
    data["environment"]["runner"]["cpu_count"] = 999
    current.write_text(json.dumps(data))

    result = _run(current)
    assert result.returncode == EXIT_NOT_SCORED, result.stdout + result.stderr
    assert "environment.runner.cpu_count" in result.stderr, result.stderr


def test_CONTROL_load_summary_distinguishes_absent_from_present():
    """So the reported string is not the same either way."""
    present = {"environment": {"runner": {"load_average": {"1min": 3.2}, "runner_name": "tim-1"}}}
    assert load_summary(present) == "1-min load 3.2 on tim-1"
    assert load_summary({"environment": {"runner": {}}}) == "load not recorded"
    assert load_summary({}) == "load not recorded"


def test_a_run_that_could_not_read_the_broker_is_refused(tmp_path: Path):
    """An unreadable broker leaves the environment block incomplete.

    The probe records `probe_failed` instead of defaulting the version,
    JetStream setting and payload limit, so the run does not know what server
    it measured against. Scoring that against a baseline taken on a known one
    reports a comparison nobody can interpret, so the gate refuses.
    """
    data = json.loads(BASELINE.read_text())
    data["environment"]["nats"] = {"probe_failed": "URLError: connection refused"}
    current = tmp_path / "current.json"
    current.write_text(json.dumps(data))

    result = _run(current)
    assert result.returncode == 1, result.stdout
    assert "probe_failed" in result.stderr or "could not read the broker" in result.stderr


def test_the_broker_refusal_is_reported_whatever_the_machine(tmp_path: Path):
    """A run that could not read the broker says so, even on a different machine.

    Both are refusals, and they call for different responses: a broker the run
    could not read is a broken run, and a different machine is a baseline to
    record. The broker check runs first, so its message is the one given.
    """
    data = json.loads(BASELINE.read_text())
    data["environment"]["nats"] = {"probe_failed": "URLError: connection refused"}
    data["environment"]["runner"]["cpu_count"] = 999
    current = tmp_path / "current.json"
    current.write_text(json.dumps(data))

    result = _run(current)
    assert result.returncode == 1, result.stdout
    assert "could not read the broker" in result.stderr, result.stderr
    assert "different machine" not in result.stderr, result.stderr


def test_CONTROL_a_run_that_read_the_broker_is_not_refused(tmp_path: Path):
    """So the refusal is about the marker, not about every current run."""
    current = tmp_path / "current.json"
    current.write_text(BASELINE.read_text())
    assert _run(current).returncode == 0


def test_an_unreadable_git_commit_is_recorded_as_unknown(monkeypatch):
    """The provenance value when git cannot be read, pinned rather than assumed.

    `get_git_commit` swallows every exception and returns a string that is
    written into the artifact as the commit it measured. Nothing read that
    path, so a change from "unknown" to an empty string or a raise would have
    travelled into the artifact unnoticed.
    """
    from tests.benchmark import benchmarks

    def _no_git(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(benchmarks.subprocess, "run", _no_git)
    assert benchmarks.get_git_commit() == "unknown"


def test_CONTROL_a_readable_git_commit_is_the_commit(monkeypatch):
    """And the ordinary path returns what git printed, so the test above is
    not passing because the function always says "unknown"."""
    from types import SimpleNamespace

    from tests.benchmark import benchmarks

    monkeypatch.setattr(
        benchmarks.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="cafef00d\n")
    )
    assert benchmarks.get_git_commit() == "cafef00d"


# --- The gate refuses to score a run taken on a busy host -------------------
#
# Recording both loads and printing a caveat, then failing anyway, reports the
# host as a change in the code. These pin the refusal, its boundary on BOTH
# sides, and that it is distinguishable from the two outcomes it is not.


def _quiet(tmp_path: Path, **runner_extra) -> Path:
    """A current run whose metrics equal the baseline, with the given runner fields."""
    data = json.loads(BASELINE.read_text())
    runner = data.setdefault("environment", {}).setdefault("runner", {})
    runner.pop("load_average", None)
    runner.pop("runner_name", None)
    runner.update(runner_extra)
    out = tmp_path / "quiet.json"
    out.write_text(json.dumps(data))
    return out


def test_a_run_taken_on_a_busy_host_is_not_scored(tmp_path: Path):
    """The reported case: current 3.5, past the limit of any baseline recorded at 1.0 or below.

    Every metric in that run moved toward slower and the two that crossed the
    threshold were the two most sensitive to contention. The gate recorded both
    loads, printed the caveat, and failed the build as a regression anyway.
    """
    recorded = one_minute_load(json.loads(BASELINE.read_text()))
    assert (
        recorded is not None and max(recorded, LOAD_REFERENCE_FLOOR) * LOAD_HEADROOM_MULTIPLE < 3.5
    ), "the committed baseline's limit no longer sits below 3.5, so this case is no longer busy"
    current = _regressed(
        tmp_path, load_average={"1min": 3.5, "5min": 3.1, "15min": 2.4}, runner_name="tim-2"
    )
    result = _run(current)

    assert result.returncode == EXIT_NOT_SCORED, (
        f"expected the not-scored exit {EXIT_NOT_SCORED}, got {result.returncode}:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "NOT SCORED" in result.stderr, result.stderr
    assert "3.5" in result.stderr and f"baseline {recorded}" in result.stderr, (
        "the refusal must name both loads, or the reader cannot tell which host "
        f"state it declined:\n{result.stderr}"
    )


def test_the_refusal_is_not_a_regression_and_not_a_pass(tmp_path: Path):
    """Three outcomes, three responses: read your diff, re-run quiet, ship it.

    A refusal that read like a regression sends the reader to a diff that has
    nothing to do with it, which is what the reported run did.
    """
    current = _regressed(
        tmp_path, load_average={"1min": 3.5, "5min": 3.1, "15min": 2.4}, runner_name="tim-2"
    )
    result = _run(current)

    assert result.returncode != 0, "a run on a busy host must not read as a pass"
    assert result.returncode != 1, (
        "the refusal shares its exit code with a regression, so nothing but the "
        "prose distinguishes them"
    )
    assert "NOT a regression" in result.stderr, result.stderr
    assert "regressed beyond" not in result.stdout, (
        f"the refusal printed the regression report as well:\n{result.stdout}"
    )


def test_a_busy_host_is_reported_as_busy_whatever_the_machine(tmp_path: Path):
    """The load and machine refusals share an exit code, so the message decides.

    The load check runs first: a busy host is re-run on a quiet one, while a
    different machine needs a baseline recorded there.
    """
    current = _regressed(
        tmp_path,
        load_average={"1min": 3.5},
        runner_name="tim-3",
        cpu_count=999,
        total_ram_gb=1,
    )
    result = _run(current)

    assert result.returncode == EXIT_NOT_SCORED, result.stdout + result.stderr
    assert "host 1-min load 3.5" in result.stderr, result.stderr
    assert "different machine" not in result.stderr, result.stderr


def test_the_limit_is_pinned_on_both_sides(tmp_path: Path):
    """A limit tested from one side only can be moved from the other.

    For a baseline recorded at a 1-min load of 1.0, `2.0` is the last load that scores and `2.01`
    the first that does not, so neither raising nor lowering the multiple leaves every test green.
    The pin is taken on a copy of the committed baseline set to 1.0, so it holds whatever load the
    committed one was recorded at; the committed baseline's own limit is then read as the checker
    reads it: twice its load, or twice 1.0 when it was recorded quieter than that.
    """
    committed = json.loads(BASELINE.read_text())
    recorded = one_minute_load(committed)
    assert recorded is not None, "the committed baseline records no 1-min load"
    own_limit = max(recorded, 1.0) * 2.0
    assert (
        load_too_high_to_score(
            committed, [{"environment": {"runner": {"load_average": {"1min": own_limit}}}}]
        )
        is None
    )
    assert load_too_high_to_score(
        committed, [{"environment": {"runner": {"load_average": {"1min": own_limit + 0.01}}}}]
    ) == (own_limit + 0.01, own_limit)

    baseline = json.loads(BASELINE.read_text())
    baseline["environment"]["runner"]["load_average"] = {"1min": 1.0}

    def at(load: float):
        return load_too_high_to_score(
            baseline, [{"environment": {"runner": {"load_average": {"1min": load}}}}]
        )

    assert at(2.0) is None, "a load exactly at the limit must still be scored"
    assert at(2.01) is not None, "a load past the limit must not be scored"
    assert at(1.99) is None
    assert at(3.5) == (3.5, 2.0)


def test_a_quiet_baseline_does_not_make_the_gate_hair_trigger(tmp_path: Path):
    """Twice almost-nothing is still almost-nothing.

    Without the floor, a baseline recorded on an unusually idle host would
    refuse ordinary quiet runs -- a gate that scores nothing rather than one
    that scores badly.
    """
    idle = {"environment": {"runner": {"load_average": {"1min": 0.1}}}}
    ordinary = {"environment": {"runner": {"load_average": {"1min": 1.9}}}}

    assert load_too_high_to_score(idle, [ordinary]) is None, (
        f"a load of 1.9 was refused against an idle baseline; the floor of "
        f"{LOAD_REFERENCE_FLOOR} is not being applied"
    )
    assert load_too_high_to_score(
        idle, [{"environment": {"runner": {"load_average": {"1min": 2.5}}}}]
    ) == (2.5, 2.0)


def test_a_current_run_with_no_load_reading_is_not_refused(tmp_path: Path):
    """An absent reading cannot be compared to a limit.

    It is already reported as absent rather than passed over, and refusing on
    it would turn a host that cannot report load into a host that can never be
    scored.
    """
    baseline = json.loads(BASELINE.read_text())
    assert load_too_high_to_score(baseline, [{"environment": {"runner": {}}}]) is None

    current = _regressed(tmp_path)
    result = _run(current)
    assert result.returncode == 1, (
        f"a run with no load reading was not scored:\n{result.stdout}\n{result.stderr}"
    )
    assert "load not recorded" in result.stdout, result.stdout


def test_CONTROL_a_load_inside_the_limit_is_still_scored(tmp_path: Path):
    """The refusal must not swallow the gate it guards.

    Without this, refusing on every run would pass every test above.
    """
    current = _regressed(tmp_path, load_average={"1min": 1.5}, runner_name="tim-2")
    result = _run(current)

    assert result.returncode == 1, (
        f"a halved metric at a load inside the limit was not scored as a "
        f"regression:\n{result.stdout}\n{result.stderr}"
    )
    assert "NOT SCORED" not in result.stderr, result.stderr


def test_CONTROL_a_quiet_run_with_baseline_metrics_passes(tmp_path: Path):
    """And the other end: the gate still says yes to a clean quiet run."""
    current = _quiet(tmp_path, load_average={"1min": 1.0}, runner_name="tim-2")
    result = _run(current)

    assert result.returncode == 0, (
        f"a quiet run at baseline metrics did not pass:\n{result.stdout}\n{result.stderr}"
    )
    assert "SUCCESS" in result.stdout, result.stdout


def test_the_multiple_and_floor_are_stated_rather_than_derived():
    """The two numbers the refusal turns on, read out loud.

    A test that recomputed them from the module would pass for any value.
    """
    assert LOAD_HEADROOM_MULTIPLE == 2.0
    assert LOAD_REFERENCE_FLOOR == 1.0
    assert EXIT_NOT_SCORED == 2

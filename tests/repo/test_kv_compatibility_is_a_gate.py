"""A supported-broker contract cannot pass merely because its tests skipped."""

import asyncio
import importlib.util
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import AsyncMock

import pytest

from tests.repo.ci_workflows import ci_workflow_paths, load

pytestmark = pytest.mark.repo

_spec = importlib.util.spec_from_file_location(
    "kv_compatibility_gate",
    Path(__file__).resolve().parents[2] / "scripts/check_kv_compatibility.py",
)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


@pytest.mark.asyncio
async def test_a_stalled_broker_probe_times_out_and_closes_its_connection(monkeypatch):
    nc = AsyncMock()

    async def stalled(*args, **kwargs):
        await asyncio.Future()

    nc.connect.side_effect = stalled
    monkeypatch.setattr(gate.nats, "NATS", lambda: nc)
    timeout = asyncio.timeout

    def short_timeout(seconds):
        assert seconds == 10
        return timeout(0.01)

    monkeypatch.setattr(gate.asyncio, "timeout", short_timeout)
    async with timeout(0.2):
        with pytest.raises(TimeoutError):
            await gate.check_broker("nats://127.0.0.1:43210")
    nc.close.assert_awaited_once()


@pytest.mark.parametrize("outcome", ["skipped", "failure", "error", "empty"])
def test_an_unexecuted_or_failed_contract_cannot_be_reported_as_green(tmp_path, outcome):
    report = tmp_path / "results.xml"
    case = "" if outcome == "empty" else f"<testcase><{outcome}/></testcase>"
    report.write_text(f"<testsuites><testsuite>{case}</testsuite></testsuites>")
    with pytest.raises(RuntimeError, match="zero failures and zero skips"):
        gate.check_report(report)


def test_success_is_counted_from_executed_cases_not_the_report_totals(tmp_path, capsys):
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuites tests="999" skipped="999"><testsuite><testcase/></testsuite></testsuites>'
    )
    gate.check_report(report)
    assert "1 passed, 0 failed, 0 skipped" in capsys.readouterr().out


@pytest.mark.parametrize("stage", ["start", "contract"])
def test_a_failed_run_still_removes_only_its_own_broker(monkeypatch, stage):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "create":
            return CompletedProcess(command, 0, stdout="owned-container\n")
        if command[1] == "start" and stage == "start":
            raise RuntimeError("start failed")
        return CompletedProcess(command, 0, stdout="127.0.0.1:43210\n")

    monkeypatch.setattr(gate.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="failed"):
        with gate.disposable_broker() as url:
            assert url == "nats://127.0.0.1:43210"
            raise RuntimeError("contract failed")
    assert commands[-1] == ["docker", "rm", "-f", "owned-container"]
    create = commands[0]
    assert create[create.index("-p") + 1] == "127.0.0.1::4222"
    assert gate.BROKER_IMAGE == "nats:2.11.2-alpine"


def test_failed_creation_cannot_remove_someone_elses_broker(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        raise RuntimeError("name already exists")

    monkeypatch.setattr(gate.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="already exists"):
        with gate.disposable_broker():
            pytest.fail("A failed create yielded a broker")
    assert len(commands) == 1
    assert commands[0][1] == "create"


@pytest.mark.parametrize("exit_code", [0, 1, 5])
def test_the_contract_runs_both_live_modules_on_the_requested_broker(monkeypatch, exit_code):
    checked = []

    async def check_broker(url):
        checked.append(url)

    def run(command, **kwargs):
        assert checked == ["nats://127.0.0.1:43210"]
        assert "tests/integration/test_kv_expiry_markers_live.py" in command
        assert "tests/integration/test_kv_bucket_options_live.py" in command
        assert kwargs["env"]["CLIFFRACER_TEST_NATS_URL"] == checked[0]
        assert kwargs["timeout"] == 120
        report = Path(
            next(
                arg.removeprefix("--junitxml=") for arg in command if arg.startswith("--junitxml=")
            )
        )
        report.write_text("<testsuites><testsuite><testcase/></testsuite></testsuites>")
        return CompletedProcess(command, exit_code)

    monkeypatch.setattr(gate, "check_broker", check_broker)
    monkeypatch.setattr(gate.subprocess, "run", run)
    if exit_code:
        with pytest.raises(RuntimeError, match=f"pytest exited {exit_code}"):
            gate.run_contract("nats://127.0.0.1:43210")
    else:
        gate.run_contract("nats://127.0.0.1:43210")


def test_each_pipeline_runs_the_contract_after_the_full_suite_and_cleans_up():
    for _, path in ci_workflow_paths():
        job = load(path)["jobs"]["test"]
        steps = job["steps"]
        contract = next(
            s for s in steps if s.get("run") == "uv run python scripts/check_kv_compatibility.py"
        )
        full = next(s for s in steps if s.get("name") == "Run tests")
        assert steps.index(contract) > steps.index(full)
        assert "if" not in contract and not contract.get("continue-on-error")
        assert contract["timeout-minutes"] <= 5
        name = job["env"]["KV_NATS_NAME"]
        assert "github.run_id" in name and "github.run_attempt" in name
        assert any(
            s.get("if") == "always()" and 'docker rm -f "$KV_NATS_NAME"' in s.get("run", "")
            for s in steps
        )

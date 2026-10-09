"""The message-schedule contract runs on a pinned 2.12 broker, and cannot pass by skipping.

Scheduled publishing needs nats-server 2.12 or later, newer than the suite's broker, so its live
rows are deselected in every run but the gate's (`scripts/check_message_schedules.py`), which
starts that broker itself and refuses a report in which any row skipped or none ran.
"""

import ast
import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.repo.ci_workflows import ci_workflow_paths, load

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
#: The address a faked port mapping yields: test data, never dialled.
FAKE_URL = "nats://127.0.0.1:43210"
_spec = importlib.util.spec_from_file_location(
    "message_schedule_gate", REPO / "scripts/check_message_schedules.py"
)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


@pytest.mark.parametrize("outcome", ["skipped", "failure", "error", "empty"])
def test_an_unexecuted_or_failed_contract_cannot_be_reported_as_green(tmp_path, outcome):
    report = tmp_path / "results.xml"
    case = "" if outcome == "empty" else f"<testcase><{outcome}/></testcase>"
    report.write_text(f"<testsuites><testsuite>{case}</testsuite></testsuites>")
    with pytest.raises(RuntimeError, match="zero failures and zero skips"):
        gate.check_report(report)


def test_the_broker_is_the_pinned_floor_on_a_free_loopback_port_and_only_it_is_removed(
    monkeypatch,
):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "create":
            return CompletedProcess(command, 0, stdout="owned-container\n")
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(gate, "free_loopback_port", lambda: 43210)
    monkeypatch.setattr(gate.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="contract failed"):
        with gate.disposable_broker() as (url, container):
            assert (url, container) == (FAKE_URL, "owned-container")
            raise RuntimeError("contract failed")

    assert commands[-1] == ["docker", "rm", "-f", "owned-container"]
    create = commands[0]
    # Fixed, not Docker's `127.0.0.1::4222`: an assigned port moves on every restart.
    assert create[create.index("-p") + 1] == "127.0.0.1:43210:4222"
    assert gate.BROKER_IMAGE == "nats:2.12.0-alpine"
    assert gate.FLOOR == (2, 12)


def test_the_port_is_one_found_free_on_loopback_by_binding_port_zero():
    port = gate.free_loopback_port()

    assert 0 < port < 65536
    with socket.socket() as again:
        again.bind(("127.0.0.1", port))  # it was released, so it binds


def test_a_port_taken_before_the_broker_binds_it_is_retried_then_refused_by_name(monkeypatch):
    commands, made = [], []
    ports = iter([40001, 40002, 40003])

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "create":
            made.append(f"c{len(commands)}")
            return CompletedProcess(command, 0, stdout=f"{made[-1]}\n")
        if command[1] == "start":
            return CompletedProcess(command, 1, stdout="", stderr="port is already allocated")
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(gate, "free_loopback_port", lambda: next(ports))
    monkeypatch.setattr(gate.subprocess, "run", run)
    with pytest.raises(RuntimeError, match=r"\[40001, 40002, 40003\] were each taken"):
        with gate.disposable_broker():
            pass

    created = [c[c.index("-p") + 1] for c in commands if c[1] == "create"]
    removed = [c[-1] for c in commands if c[1] == "rm"]
    assert created == [f"127.0.0.1:{p}:4222" for p in (40001, 40002, 40003)]
    assert removed == made, "a container it did not create was removed, or one was kept"


def test_CONTROL_a_start_that_fails_for_another_reason_is_not_retried(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "create":
            return CompletedProcess(command, 0, stdout="c1\n")
        if command[1] == "start":
            return CompletedProcess(command, 1, stdout="", stderr="no such image")
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(gate, "free_loopback_port", lambda: 40001)
    monkeypatch.setattr(gate.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="did not start: no such image"):
        with gate.disposable_broker():
            pass

    assert sum(c[1] == "create" for c in commands) == 1


def _contract_run(monkeypatch, container):
    checked, runs = [], []

    async def check_broker(url):
        checked.append(url)

    def run(command, **kwargs):
        runs.append((command, kwargs["env"]))
        report = Path(
            next(a.removeprefix("--junitxml=") for a in command if a.startswith("--junitxml="))
        )
        report.write_text("<testsuites><testsuite><testcase/></testsuite></testsuites>")
        return CompletedProcess(command, 0)

    monkeypatch.setattr(gate, "check_broker", check_broker)
    monkeypatch.setattr(gate.subprocess, "run", run)
    gate.run_contract(FAKE_URL, container)
    return checked, runs


def test_the_contract_collects_the_live_rows_on_the_requested_broker_and_its_container(
    monkeypatch,
):
    checked, runs = _contract_run(monkeypatch, "owned-container")

    ((command, env),) = runs
    assert checked == [FAKE_URL]
    assert gate.CONTRACT in command and "-k" not in command, "the restart rows were left out"
    assert env["CLIFFRACER_TEST_NATS_URL"] == FAKE_URL
    assert env["CLIFFRACER_TEST_MESSAGE_SCHEDULES"] == "1"
    assert env[gate.BROKER_ENV] == "owned-container"


def test_with_a_broker_it_does_not_manage_the_restart_rows_are_not_collected(monkeypatch, capsys):
    _, runs = _contract_run(monkeypatch, None)

    ((command, env),) = runs
    assert command[command.index("-k") + 1] == f"not ({gate.RESTART_ROWS})"
    assert gate.BROKER_ENV not in env
    assert "the broker-restart rows are not collected" in capsys.readouterr().out


def _docker_calls(tree: ast.AST) -> list[tuple[ast.Call, list[ast.expr]]]:
    """Each `_docker(...)` call, or `asyncio.to_thread(_docker, ...)`, with the docker arguments."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) == "_docker":
            calls.append((node, list(node.args)))
        elif node.args and getattr(node.args[0], "id", None) == "_docker":
            calls.append((node, list(node.args[1:])))
    return calls


def _docker_targets(source: str) -> list[tuple[int, str]]:
    """Each docker call in `source` whose container argument is not `container`, the name
    `_the_gates_broker()` returns, as (line, the call)."""
    strays = []
    for node, args in _docker_calls(ast.parse(source)):
        target = args[1] if len(args) > 1 else None
        if not (isinstance(target, ast.Name) and target.id == "container"):
            strays.append((node.lineno, ast.unparse(node)))
    return strays


def test_the_restart_rows_touch_only_the_gates_container():
    source = (REPO / gate.CONTRACT).read_text()
    tree = ast.parse(source)
    rows = [
        f
        for f in ast.walk(tree)
        if isinstance(f, ast.AsyncFunctionDef) and f.name.startswith("test_") and _docker_calls(f)
    ]

    assert len(_docker_calls(tree)) >= 3, "the restart rows' docker calls were not read"
    assert len(rows) == 2, [row.name for row in rows]
    assert _docker_targets(source) == []
    for row in rows:
        assert "container = _the_gates_broker()" in ast.unparse(row), row.name


def test_CONTROL_a_docker_call_on_another_name_is_found():
    planted = (
        "async def row():\n"
        "    container = _the_gates_broker()\n"
        '    await asyncio.to_thread(_docker, "restart", container)\n'
        '    await asyncio.to_thread(_docker, "stop", "someone-elses")\n'
    )

    assert _docker_targets(planted) == [(4, "asyncio.to_thread(_docker, 'stop', 'someone-elses')")]


def _collected(**env: str) -> str:
    """The collect-only summary of the live file, run with no broker named."""
    child = {k: v for k, v in os.environ.items() if not k.startswith("CLIFFRACER_TEST_")}
    child.update(env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            gate.CONTRACT,
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            # The suite's own `-q` doubled would list no node ids, which this counts.
            "-o",
            "addopts=--strict-markers --import-mode=importlib",
        ],
        cwd=REPO,
        env=child,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout


def test_the_live_rows_are_deselected_unless_the_gate_asks_for_them():
    def rows(output: str) -> int:
        return sum(1 for line in output.splitlines() if f"{gate.CONTRACT}::" in line)

    unasked = _collected()
    asked = _collected(CLIFFRACER_TEST_MESSAGE_SCHEDULES="1")

    assert (rows(unasked), rows(asked)) == (0, 11), (unasked, asked)
    assert "11 deselected" in unasked


def test_each_pipeline_runs_the_contract_after_the_full_suite():
    for _, path in ci_workflow_paths():
        steps = load(path)["jobs"]["test"]["steps"]
        contract = next(
            s for s in steps if s.get("run") == "uv run python scripts/check_message_schedules.py"
        )
        full = next(s for s in steps if s.get("name") == "Run tests")
        assert steps.index(contract) > steps.index(full)

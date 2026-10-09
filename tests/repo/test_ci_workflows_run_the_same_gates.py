"""Every CI workflow in the repository runs the same gates.

Two platforms are real. Gitea runs CI here; GitHub runs it on the upstream this
tree is merged into. Each has one workflow, and both are asserted against the
same invariants, so a change that neuters one is not hidden by the other being
intact.

Gates are read out of the parsed step list, so text in a comment does not count
as a command, and only steps that always execute count, so a gate parked behind
an `if:` does not count either. Matching is by equality: a gate with anything
appended, a success-swallowing `|| true` above all, is a different command.

Where the two files legitimately differ -- the runner image the benchmark job
wants, the artifact action Gitea needs, the extra job Gitea carries -- the
divergence is recorded below and asserted, so an unexplained one reds.
"""

from pathlib import Path
from typing import Any

import pytest

from tests.repo.ci_workflows import (
    PLATFORM_DIRS,
    ci_workflow_files,
    ci_workflow_ids,
    ci_workflow_paths,
    load,
    rel,
)

pytestmark = pytest.mark.repo

# The pipeline rules below apply to CI pipelines. A workflow with neither a
# push nor a pull_request trigger is not gating a change, so it has no test
# job and nothing to compare across platforms; `ci_workflows.is_ci_pipeline`
# derives that from the triggers, and
# tests/repo/test_a_scheduled_workflow_is_not_a_pipeline.py holds the rules
# that DO apply to one.
WORKFLOWS = ci_workflow_paths()
WORKFLOW_IDS = ci_workflow_ids()

# Matched verbatim, so a gate that changes shape has to be changed here too --
# which is the point: the list is the record of what a platform must run.
#
# The pytest gate carries `-m "not benchmark"`. The benchmark tier's assertions
# include throughput floors against fixed numbers, and the test job's runner is
# shared with everything else on its host, so a busy machine crossed them and
# blocked a release gated on this job. The tier runs in the benchmark job
# instead, where the host's load is known; `tests/repo/test_benchmark_job_runs_alone.py`
# holds the rules about that job.
REQUIRED_GATE_COMMANDS = [
    "uv run python scripts/check_changelog_fragment.py",
    "uv run python scripts/check_kv_compatibility.py",
    "uv run ruff check src/ packages/ tests/ examples/ load-testing/ scripts/",
    "uv run ruff format --check src/ packages/ tests/ examples/ load-testing/ scripts/",
    "uv run mypy src/ packages/*/src",
    'uv run pytest -m "not benchmark" -p no:cacheprovider --tb=short',
]

# Divergences that are meant, with the reason. Anything else differing between
# the two platforms is drift.
EXPECTED_JOBS: dict[str, tuple[set[str], str]] = {
    "gitea": (
        {"test", "test-3_12", "benchmark", "release"},
        "Gitea has a runner labelled for benchmarking, so the measurement runs "
        "as its own job on that host.",
    ),
    "github": (
        {"test", "test-3_12", "release"},
        "GitHub's hosted runners are shared and noisy, so no benchmark job is defined there.",
    ),
}

EXPECTED_SETUP_UV: dict[str, tuple[str, str]] = {
    "gitea": (
        "astral-sh/setup-uv@v3",
        "The Gitea runner resolves actions through its own mirror, which carries v3.",
    ),
    "github": (
        "astral-sh/setup-uv@v5",
        "GitHub resolves upstream, where v5 is current.",
    ),
}


# The branch lists each platform builds on, and why they are not the same set.
# `on:` parses as the boolean True under YAML 1.1, so the triggers live at
# data[True] rather than data["on"].
EXPECTED_PUSH_BRANCHES: dict[str, tuple[list[str], str]] = {
    "gitea": (
        ["main", "release/**"],
        "A push to a feature branch that also has a pull request open would run "
        "this workflow twice in two concurrency groups, so Gitea builds only the "
        "branches that are not covered by the pull_request trigger.",
    ),
    "github": (
        ["main", "release/**", "feat/**", "fix/**"],
        "GitHub is the published mirror, where a feature branch is built on push "
        "so a contributor without pull-request rights still gets a result.",
    ),
}

# The pull_request trigger is the same on both, and there is no reason for it
# to differ: a proposed change is built the same way wherever it is proposed.
EXPECTED_PULL_REQUEST_BRANCHES = ["main", "release/**"]


def triggers(workflow_data: dict[str, Any]) -> dict[str, Any]:
    """The `on:` block, which YAML 1.1 parses as the key True."""
    on = workflow_data.get(True, workflow_data.get("on"))
    assert isinstance(on, dict), "the workflow declares no trigger block"
    return on


def test_the_platforms_build_the_same_proposed_changes():
    """The pull_request branch list is not a place the platforms may drift.

    A branch built on one platform and not the other is a gate that exists for
    half the changes, which is the shape this file's name promises against.
    """
    for _platform, path in WORKFLOWS:
        branches = triggers(load(path)).get("pull_request", {}).get("branches")
        assert branches == EXPECTED_PULL_REQUEST_BRANCHES, (
            f"{rel(path)} builds pull requests for {branches}, and every platform "
            f"must build {EXPECTED_PULL_REQUEST_BRANCHES}."
        )


def test_the_push_triggers_differ_only_where_it_is_recorded():
    """Each platform's push branch list, with the reason it is what it is."""
    for platform, path in WORKFLOWS:
        expected, why = EXPECTED_PUSH_BRANCHES[platform]
        branches = triggers(load(path)).get("push", {}).get("branches")
        assert branches == expected, (
            f"{rel(path)} builds pushes to {branches}, recorded as {expected}. "
            f"{why} Update the record with the reason if the rule genuinely changed."
        )


def unconditional_test_step_commands(workflow_data: dict[str, Any]) -> list[str]:
    """Return the run-script lines of test-job steps that always execute.

    A step carrying an `if:` can be skipped, so a command parked behind one is
    not something CI is guaranteed to run. A job-level `if:` is a different
    thing and is not read here: it selects which events run the job, not
    whether the gates inside it execute when it does.
    """
    test_job = workflow_data.get("jobs", {}).get("test", {})
    commands: list[str] = []
    for step in test_job.get("steps", []):
        if "if" in step:
            continue
        run_cmd = step.get("run")
        if isinstance(run_cmd, str):
            commands.extend(line.strip() for line in run_cmd.splitlines() if line.strip())
    return commands


def gates_present(commands: list[str]) -> set[str]:
    """Return which required gates appear verbatim among `commands`."""
    return {gate for gate in REQUIRED_GATE_COMMANDS if gate in commands}


def actions_used(workflow_data: dict[str, Any]) -> set[str]:
    """Return every `uses:` reference in the workflow."""
    return {
        str(step["uses"])
        for job in workflow_data.get("jobs", {}).values()
        for step in job.get("steps", [])
        if step.get("uses")
    }


def test_every_platform_holds_exactly_one_workflow():
    """One workflow per platform, so a second file cannot add or bypass gates unseen."""
    assert WORKFLOWS, "no CI pipeline was discovered at all"
    for platform, directory in PLATFORM_DIRS.items():
        files = ci_workflow_files(platform)
        assert len(files) == 1, (
            f"{platform} has {len(files)} CI pipelines in {directory} "
            f"({[p.name for p in files]}); gates are asserted against one per platform. "
            f"A workflow that is not CI -- no push or pull_request trigger -- does not "
            f"count here and is governed by "
            f"tests/repo/test_a_scheduled_workflow_is_not_a_pipeline.py instead."
        )


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_the_workflow_runs_every_gate_unconditionally(platform: str, path: Path):
    """Each workflow's test job runs all required gates in steps that always execute."""
    data = load(path)
    assert "test" in data.get("jobs", {}), f"{rel(path)} has no 'test' job"

    commands = unconditional_test_step_commands(data)
    missing = set(REQUIRED_GATE_COMMANDS) - gates_present(commands)
    assert not missing, (
        f"{rel(path)} does not run {sorted(missing)} as an unconditional step "
        f"(executed: {commands})"
    )


def test_the_platforms_run_the_same_gates():
    """The gate set is the thing that must match across platforms."""
    by_platform = {
        platform: gates_present(unconditional_test_step_commands(load(path)))
        for platform, path in WORKFLOWS
    }
    distinct = {frozenset(v) for v in by_platform.values()}
    assert len(distinct) == 1, "the platforms run different gates: " + "; ".join(
        f"{k}={sorted(v)}" for k, v in sorted(by_platform.items())
    )


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_the_workflow_runs_its_test_job_on_a_pinned_runner(platform: str, path: Path):
    """The test job names the runner image its gates expect."""
    test_job = load(path).get("jobs", {}).get("test", {})
    assert test_job.get("runs-on") == "ubuntu-latest", (
        f"{rel(path)}: test runs on {test_job.get('runs-on')!r}, not ubuntu-latest"
    )


def test_the_platforms_differ_only_where_it_is_recorded():
    """Job topology and runner setup differ by design; anything else is drift."""
    for platform, path in WORKFLOWS:
        data = load(path)

        expected_jobs, why_jobs = EXPECTED_JOBS[platform]
        assert set(data.get("jobs", {})) == expected_jobs, (
            f"{rel(path)} defines jobs {sorted(data.get('jobs', {}))}, recorded as "
            f"{sorted(expected_jobs)}. {why_jobs} Update the record with the reason "
            "if the topology genuinely changed."
        )

        expected_uv, why_uv = EXPECTED_SETUP_UV[platform]
        setup = {u for u in actions_used(data) if u.startswith("astral-sh/setup-uv@")}
        assert setup == {expected_uv}, (
            f"{rel(path)} uses {sorted(setup)}, recorded as {expected_uv}. {why_uv}"
        )


# Scripts that score a measurement against a recorded baseline. The baseline is
# taken on one machine, so comparing against it only means something on a host
# of that class -- which is why the benchmark runs as its own job on a labelled
# runner. In a test job it gates every pull request on hardware the runner does
# not have.
BENCHMARK_SCRIPTS = ("run_benchmarks", "check_benchmark_regression")


def commands_in_test_job(workflow_data: dict[str, Any]) -> list[str]:
    """Every run script in the test job, whether or not the step is conditional.

    Wider than `unconditional_test_step_commands`, which exists to check that a
    gate is not parked behind an `if:`. A benchmark step parked behind one is
    still a benchmark step in the wrong job, so this reads them all. It also
    keeps each `run` block whole rather than splitting it into lines, so a
    command broken across a continuation still matches.

    It finds the job by the literal name `test`, so a workflow that renamed
    that job would yield nothing here and pass. What stops that is
    `EXPECTED_JOBS` in `test_the_platforms_differ_only_where_it_is_recorded`,
    which pins both platforms' job sets -- this rule is load-bearing only in
    company with that one.
    """
    return [
        str(step.get("run", ""))
        for step in workflow_data.get("jobs", {}).get("test", {}).get("steps", [])
    ]


def benchmark_offenders(workflow_data: dict[str, Any]) -> list[tuple[str, str]]:
    """Every (script, run script) pair where a test-job step scores a benchmark.

    The rule and its controls both go through here. A control that re-states
    the match instead exercises only the reading of the steps: the rule can be
    broken -- `in` narrowed to `==`, say -- and a control carrying its own copy
    of the comparison still reports the offending step and still passes.
    """
    return [
        (script, command)
        for command in commands_in_test_job(workflow_data)
        for script in BENCHMARK_SCRIPTS
        if script in command
    ]


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_no_test_job_scores_a_benchmark(platform: str, path: Path):
    """A hardware comparison does not belong in the job every change runs.

    The baseline records one machine's numbers. A job that runs on whatever
    host is free cannot meet them, so scoring there fails changes for where
    they were built rather than for what they do.
    """
    offenders = benchmark_offenders(load(path))
    assert not offenders, (
        f"{rel(path)} scores a benchmark inside its test job: {offenders}. "
        "A measurement belongs in a job on a runner of the baseline's class."
    )


def test_CONTROL_a_benchmark_step_in_a_test_job_is_rejected(tmp_path: Path):
    """The rule above must see a benchmark step when one is there."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "on:\n  push:\n    branches: [ main ]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: uv run pytest\n"
        "      - run: uv run python scripts/check_benchmark_regression.py --threshold 0.15\n"
    )
    offenders = benchmark_offenders(load(workflow))
    assert [script for script, _ in offenders] == ["check_benchmark_regression"], offenders


def test_CONTROL_a_test_job_without_one_reports_nothing(tmp_path: Path):
    """And it is not simply reporting every step it sees."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "on:\n  push:\n    branches: [ main ]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: uv run pytest\n"
    )
    assert not benchmark_offenders(load(workflow))


def test_CONTROL_commented_out_gate_is_rejected():
    """Negative control: a gate surviving only as a YAML comment is not executed."""
    sample_yaml = """
name: CI
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Disabled lint
        run: echo GATE-DISABLED  # uv run ruff check src/ packages/ tests/ examples/ load-testing/ scripts/
"""
    import yaml

    commands = unconditional_test_step_commands(yaml.safe_load(sample_yaml))
    assert gates_present(commands) == set()


def test_CONTROL_a_gate_that_cannot_fail_is_rejected():
    """Negative control: `|| true` makes a gate unable to fail, so it is not the gate."""
    sample_yaml = """
name: CI
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Type check
        run: uv run mypy src/ packages/*/src || true
"""
    import yaml

    commands = unconditional_test_step_commands(yaml.safe_load(sample_yaml))
    assert gates_present(commands) == set()


def test_CONTROL_a_gate_parked_in_a_skipped_step_is_rejected():
    """Negative control: a step that never runs does not count as running its gate."""
    sample_yaml = """
name: CI
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Real work
        run: echo NEUTERED
      - name: never runs
        if: false
        run: uv run mypy src/ packages/*/src
"""
    import yaml

    commands = unconditional_test_step_commands(yaml.safe_load(sample_yaml))
    assert gates_present(commands) == set()


def test_CONTROL_a_job_level_if_does_not_hide_the_gates():
    """Negative control on the other side: selecting events is not skipping steps.

    A job-level `if:` decides which events run the job. The gates inside it
    still run when it does, so it must not make them invisible here.
    """
    sample_yaml = """
name: CI
on: [push, workflow_dispatch]
jobs:
  test:
    runs-on: ubuntu-latest
    if: github.event_name != 'workflow_dispatch'
    steps:
      - name: Type check with mypy
        run: uv run mypy src/ packages/*/src
"""
    import yaml

    commands = unconditional_test_step_commands(yaml.safe_load(sample_yaml))
    assert gates_present(commands) == {"uv run mypy src/ packages/*/src"}


def test_CONTROL_the_real_gates_are_matched_when_present():
    """Positive control: the matcher does find a gate written the way CI writes it."""
    sample_yaml = """
name: CI
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Type check with mypy
        run: uv run mypy src/ packages/*/src
"""
    import yaml

    commands = unconditional_test_step_commands(yaml.safe_load(sample_yaml))
    assert gates_present(commands) == {"uv run mypy src/ packages/*/src"}


PYTEST_GATE = 'uv run pytest -m "not benchmark" -p no:cacheprovider --tb=short'


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_the_suite_also_runs_on_python_3_12_and_the_release_needs_it(platform: str, path: Path):
    """The lowest Python the packages declare is tested on both platforms, the same way.

    `test-3_12` pins the interpreter for every `uv` command through `UV_PYTHON`, asserts it before
    the suite, and runs the pytest gate unconditionally against a broker it starts. The release job
    lists it among its needs, so a suite that fails on 3.12 alone publishes nothing, and so does the
    benchmark job where there is one, so the measurement is not taken beside it.
    """
    jobs = load(path)["jobs"]
    job = jobs.get("test-3_12")
    assert job is not None, f"{rel(path)} has no test-3_12 job"
    assert job.get("name") == "test-3.12", job.get("name")
    assert str(job.get("env", {}).get("UV_PYTHON")) == "3.12", job.get("env")
    commands = [
        line.strip()
        for step in job.get("steps", [])
        if "if" not in step and isinstance(step.get("run"), str)
        for line in step["run"].splitlines()
        if line.strip()
    ]
    assert "uv python install 3.12" in commands, commands
    assert PYTEST_GATE in commands, commands
    assert any("sys.version_info[:2] == (3, 12)" in command for command in commands), commands
    runs = [
        step
        for step in job["steps"]
        if isinstance(step.get("run"), str)
        and PYTEST_GATE in (line.strip() for line in step["run"].splitlines())
    ]
    assert runs and runs[0].get("env", {}).get("CLIFFRACER_TEST_NATS_URL"), (
        "test-3_12 must name the broker it dials"
    )
    assert any(command.startswith("docker run ") for command in commands), (
        "test-3_12 must start a broker of its own"
    )
    needs = jobs["release"].get("needs")
    needs = [needs] if isinstance(needs, str) else list(needs or [])
    assert "test-3_12" in needs, f"{rel(path)}: release needs {needs}"
    if "benchmark" in jobs:
        bench = jobs["benchmark"].get("needs")
        bench = [bench] if isinstance(bench, str) else list(bench or [])
        assert "test-3_12" in bench, f"{rel(path)}: benchmark needs {bench}"

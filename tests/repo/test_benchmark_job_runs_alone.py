"""Verify the benchmark job measures a host with nothing else running on it.

The benchmark compares against a recorded baseline and fails a metric that
moves more than 15%. Anything sharing the host moves them further than that:
two benchmark jobs together move rpc p50 by up to 26% and throughput by 19%.

Three things keep it alone. It runs only on a dispatch or a push to main, so a
pull_request and a push for the same branch cannot both reach it. `needs: test`
orders it behind the suite on a main push rather than letting the scheduler put
both on the host at once. The constant concurrency group covers the case the
event gating cannot: a fast-forward puts one commit under two events.
"""

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".gitea" / "workflows" / "ci.yml"

DISPATCH = "workflow_dispatch"
MAIN = "refs/heads/main"
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")

# The needed-job outcomes the release condition distinguishes. The suite runs
# twice, `test` on 3.13 and `test-3_12` on 3.12, and both are the suite.
SUITE_GREEN_BENCHMARK_GREEN = {"test": "success", "test-3_12": "success", "benchmark": "success"}
SUITE_GREEN_BENCHMARK_SKIPPED = {"test": "success", "test-3_12": "success", "benchmark": "skipped"}
SUITE_GREEN_BENCHMARK_FAILED = {"test": "success", "test-3_12": "success", "benchmark": "failure"}
SUITE_FAILED = {"test": "failure", "test-3_12": "success", "benchmark": "skipped"}
SUITE_FAILED_ON_3_12 = {"test": "success", "test-3_12": "failure", "benchmark": "skipped"}
# A dispatch skips the test jobs by their own condition, so the benchmark's
# needs are NOT met and only an opt-out reaches it.
DISPATCH_RESULTS = {"test": "skipped", "test-3_12": "skipped", "benchmark": "skipped"}


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(data: dict) -> dict:
    """Return the workflow's `on:` mapping.

    YAML resolves a bare `on` key to the boolean True, so the triggers live
    under `data[True]`. Reading `data.get("on")` returns None instead, and a
    check written against that passes because it found nothing.
    """
    assert True in data, (
        f"no trigger mapping under the boolean key; top-level keys are {sorted(map(str, data))}"
    )
    return data[True]


def condition_holds(
    job: dict,
    event: str,
    ref: str,
    *,
    disable_benchmarks: str = "",
    level: str = "none",
    results: dict[str, str] | None = None,
    cancelled: bool = False,
) -> bool:
    """Evaluate the job's `if:` for one state of the world, rather than matching text.

    This answers whether the CONDITION holds, which is not the same question as
    whether the runner reaches the job -- see `reaches`. Reading one for the
    other is what let two assertions in this file pass while a dispatch could
    not reach the benchmark at all.

    A substring check would accept a condition that mentions the right names in
    the wrong shape. This substitutes the values the conditions read and
    evaluates the boolean, so the answer is the one the runner would reach.

    Five kinds of value are read. The event and the ref; the
    `CLIFFRACER_DISABLE_BENCHMARKS` repository variable, which gates the
    benchmark on a push; the `level` dispatch input, which decides whether a
    dispatch is a measurement or a release and so separates the two jobs a
    dispatch can reach; the result of each needed job, which the release
    condition reads so that a *skipped* benchmark does not withhold a release
    while a *failed* one still does; and whether the run was cancelled.

    Every one is a parameter rather than a constant, so a condition is asserted
    in both states rather than in whichever one happens to pass.
    """
    condition = job.get("if")
    if condition is None:
        return True
    outcomes = results or {}
    python = condition.replace("github.event_name", repr(event)).replace("github.ref", repr(ref))
    python = python.replace("vars.CLIFFRACER_DISABLE_BENCHMARKS", repr(disable_benchmarks))
    python = python.replace("inputs.level", repr(level))
    for name, outcome in outcomes.items():
        python = python.replace(f"needs.{name}.result", repr(outcome))
    python = python.replace("!cancelled()", repr(not cancelled))
    python = python.replace("||", " or ").replace("&&", " and ")
    for leftover in ("vars.", "needs.", "inputs.", "cancelled("):
        assert leftover not in python, (
            f"the condition reads {leftover!r}, which this evaluator does not "
            f"substitute, so the result would not be the runner's: {condition!r}"
        )
    return bool(eval(python, {"__builtins__": {}}, {}))  # noqa: S307 - our own workflow file


# Conditions that opt a job out of needs-skip propagation. A job whose needed
# job did not succeed is skipped, unless its condition names one of these.
_SKIP_OPT_OUTS = ("always()", "!cancelled()")


def opts_out_of_skip_propagation(job: dict) -> bool:
    """Whether this job runs regardless of what its needed jobs did."""
    return any(token in (job.get("if") or "") for token in _SKIP_OPT_OUTS)


def needs_met(job: dict, results: dict[str, str] | None) -> bool:
    """Whether the runner would start this job given what its needed jobs did.

    A skipped need skips the dependent job. That rule is invisible to the
    condition, which is why it has to be modelled separately: a job can have a
    condition that holds and still never run.
    """
    declared = job.get("needs")
    names = [declared] if isinstance(declared, str) else list(declared or [])
    if not names:
        return True
    if opts_out_of_skip_propagation(job):
        return True
    outcomes = results or {}
    return all(outcomes.get(name) == "success" for name in names)


def reaches(
    job: dict,
    event: str,
    ref: str,
    *,
    disable_benchmarks: str = "",
    level: str = "none",
    results: dict[str, str] | None = None,
    cancelled: bool = False,
) -> bool:
    """Whether the runner actually reaches this job.

    The condition holding is necessary and not sufficient: the job is skipped
    anyway if a needed job did not succeed and the condition does not opt out.
    Every assertion about what runs should read this rather than the condition.
    """
    if not condition_holds(
        job,
        event,
        ref,
        disable_benchmarks=disable_benchmarks,
        level=level,
        results=results,
        cancelled=cancelled,
    ):
        return False
    return needs_met(job, results)


def serialises_host_wide(job: dict) -> bool:
    """True when the job's concurrency group is the same for every run."""
    concurrency = job.get("concurrency")
    if not isinstance(concurrency, dict):
        return False
    group = concurrency.get("group")
    return isinstance(group, str) and bool(group) and not EXPRESSION.search(group)


@pytest.mark.gitea_checkout
def test_the_workflow_can_be_dispatched():
    """The control under every check below: a trigger set without the dispatch
    would make "runs on a dispatch" unreachable rather than satisfied."""
    assert DISPATCH in triggers(workflow()), (
        "the benchmark job is reachable from a dispatch, so the workflow must declare one"
    )


@pytest.mark.gitea_checkout
def test_a_dispatch_reaches_the_benchmark():
    assert reaches(
        workflow()["jobs"]["benchmark"],
        DISPATCH,
        "refs/heads/anything",
        results=DISPATCH_RESULTS,
    )


@pytest.mark.gitea_checkout
def test_a_push_to_main_reaches_the_benchmark():
    """The release path: a release is gated on this job, so it has to run there."""
    assert reaches(
        workflow()["jobs"]["benchmark"], "push", MAIN, results=SUITE_GREEN_BENCHMARK_GREEN
    )


@pytest.mark.gitea_checkout
def test_no_other_event_reaches_the_benchmark():
    """The check that matters. A pull_request and a push for one branch both
    reaching this job is what put two measurements on the host at once."""
    job = workflow()["jobs"]["benchmark"]
    for event, ref in (
        ("pull_request", MAIN),
        ("pull_request", "refs/heads/release/sprint-1"),
        ("push", "refs/heads/release/sprint-1"),
        ("push", "refs/heads/fix/anything"),
    ):
        assert not reaches(job, event, ref, results=SUITE_GREEN_BENCHMARK_GREEN), (
            f"{event} on {ref} reaches the benchmark job"
        )


@pytest.mark.gitea_checkout
def test_the_benchmark_waits_for_the_suite():
    """On a main push the suites and the benchmark are triggered; `needs` is what
    stops them sharing the host. Both suites, the 3.13 and the 3.12 run.

    Equality rather than membership. A second entry here would be another job
    the measurement waits behind, which changes what else can be on the box
    when it runs -- a topology change someone should read, not one this check
    absorbs.
    """
    assert workflow()["jobs"]["benchmark"].get("needs") == ["test", "test-3_12"]


DISABLED = "true"


@pytest.mark.gitea_checkout
def test_a_push_does_not_reach_the_benchmark_when_the_variable_is_set():
    """The switch this exists for: a push stops measuring on a shared host."""
    job = workflow()["jobs"]["benchmark"]
    assert not reaches(
        job, "push", MAIN, disable_benchmarks=DISABLED, results=SUITE_GREEN_BENCHMARK_GREEN
    )


def test_CONTROL_a_condition_can_hold_while_the_runner_skips_the_job():
    """The reading that made two assertions in this file pass on a dead route.

    A job needing a skipped job is skipped, however its condition evaluates.
    Asserting the condition and calling it reachability is why a dispatch was
    documented as running the benchmark for as long as it could not.
    """
    dead_route = {"needs": "test", "if": "github.event_name == 'workflow_dispatch'"}
    skipped_suite = {"test": "skipped"}

    assert condition_holds(dead_route, DISPATCH, MAIN, results=skipped_suite), (
        "the condition itself holds -- this is what the old reading saw"
    )
    assert not reaches(dead_route, DISPATCH, MAIN, results=skipped_suite), (
        "and the runner does not reach the job, which is what it missed"
    )


def test_CONTROL_an_opt_out_is_what_makes_a_skipped_need_survivable():
    """`!cancelled()` is the difference, and it is read rather than assumed."""
    without = {"needs": "test", "if": "github.event_name == 'workflow_dispatch'"}
    with_opt_out = {
        "needs": "test",
        "if": "!cancelled() && github.event_name == 'workflow_dispatch'",
    }
    skipped_suite = {"test": "skipped"}

    assert not needs_met(without, skipped_suite)
    assert needs_met(with_opt_out, skipped_suite)
    assert not reaches(without, DISPATCH, MAIN, results=skipped_suite)
    assert reaches(with_opt_out, DISPATCH, MAIN, results=skipped_suite)


@pytest.mark.gitea_checkout
def test_a_push_still_requires_the_suite_to_have_passed():
    """Opting out of skip propagation also opts out of the implicit ordering.

    `needs: test` alone used to mean the benchmark could not start until the
    suite had passed. Once the condition says `!cancelled()`, that is no longer
    implied, so the push branch has to state it -- otherwise the measurement
    could run beside a failing suite on the same host.
    """
    job = workflow()["jobs"]["benchmark"]
    assert not reaches(job, "push", MAIN, results=SUITE_FAILED), (
        "the benchmark would run on a push whose suite failed"
    )


@pytest.mark.gitea_checkout
def test_a_dispatch_still_reaches_the_benchmark_when_the_variable_is_set():
    """A dispatch is a measurement someone asked for, so the switch spares it.

    Without this the switch would take the deliberate measurement away with the
    incidental one, and there would be no way to score a host at all.
    """
    job = workflow()["jobs"]["benchmark"]
    assert reaches(job, DISPATCH, MAIN, disable_benchmarks=DISABLED, results=DISPATCH_RESULTS)


@pytest.mark.gitea_checkout
def test_the_variable_gates_on_presence_rather_than_on_a_value():
    """Any non-empty setting disables it, so no particular contents are magic.

    A gate keyed on one string quietly does nothing when someone sets the
    variable to anything else, which is the failure a reader cannot see.
    """
    job = workflow()["jobs"]["benchmark"]
    for setting in ("true", "1", "yes", "disabled", "no"):
        assert not reaches(
            job,
            "push",
            MAIN,
            disable_benchmarks=setting,
            results=SUITE_GREEN_BENCHMARK_GREEN,
        ), f"a push still reached the benchmark with the variable set to {setting!r}"


@pytest.mark.gitea_checkout
def test_a_skipped_benchmark_does_not_withhold_a_release():
    """The other half of the switch: gating the measurement must not gate publishing."""
    job = workflow()["jobs"]["release"]
    assert reaches(job, "push", MAIN, results=SUITE_GREEN_BENCHMARK_SKIPPED)


@pytest.mark.gitea_checkout
def test_a_failed_benchmark_still_withholds_a_release():
    """A measurement that ran and failed is a regression, and still stops it."""
    job = workflow()["jobs"]["release"]
    assert not reaches(job, "push", MAIN, results=SUITE_GREEN_BENCHMARK_FAILED)


@pytest.mark.gitea_checkout
def test_a_failed_suite_withholds_a_release():
    """Correctness is not waived by any of this."""
    job = workflow()["jobs"]["release"]
    assert not reaches(job, "push", MAIN, results=SUITE_FAILED)


@pytest.mark.gitea_checkout
def test_a_cancelled_run_does_not_release():
    job = workflow()["jobs"]["release"]
    assert not reaches(job, "push", MAIN, results=SUITE_GREEN_BENCHMARK_GREEN, cancelled=True)


def test_CONTROL_the_evaluator_refuses_a_condition_it_cannot_substitute():
    """An unsubstituted name would evaluate to something that is not the runner's.

    Without this the evaluator could silently read a future `vars.` or `needs.`
    reference as a NameError-free constant and report a reachability that the
    runner does not share.
    """
    with pytest.raises(AssertionError, match="does not substitute"):
        reaches({"if": "vars.SOMETHING_ELSE == ''"}, "push", MAIN)


@pytest.mark.gitea_checkout
def test_a_release_waits_for_the_benchmark():
    """A performance regression blocks a release."""
    needs = workflow()["jobs"]["release"].get("needs")
    assert isinstance(needs, list) and {"test", "test-3_12", "benchmark"} <= set(needs), (
        f"release declares needs: {needs!r}"
    )


@pytest.mark.gitea_checkout
def test_a_measurement_dispatch_runs_nothing_beside_the_benchmark():
    """`level: none` is the measurement dispatch: the benchmark and nothing else."""
    jobs = workflow()["jobs"]
    for name in ("test", "test-3_12", "release"):
        assert not reaches(
            jobs[name], DISPATCH, MAIN, level="none", results=SUITE_GREEN_BENCHMARK_GREEN
        ), f"the {name} job would run on a measurement dispatch alongside the benchmark"


@pytest.mark.gitea_checkout
@pytest.mark.parametrize("level", ["patch", "minor"])
def test_a_release_dispatch_scores_no_benchmark(level: str):
    """A release dispatch must not put a measurement on the host.

    The benchmark is the reason this file exists: it needs a quiet host, and a
    release dispatch is not a request for a reading. Were the benchmark to run
    here it would also gate the release on a number nobody asked for.
    """
    assert not reaches(
        workflow()["jobs"]["benchmark"],
        DISPATCH,
        MAIN,
        level=level,
        results=DISPATCH_RESULTS,
    )


@pytest.mark.gitea_checkout
@pytest.mark.parametrize("level", ["patch", "minor"])
def test_a_release_dispatch_runs_the_suite_before_the_release(level: str):
    """Nothing is published without the tests having passed.

    The release job requires `needs.test.result == 'success'`, and a job that
    was SKIPPED did not succeed. So the suite has to actually run on a release
    dispatch: a test job that skipped itself here is not a faster release, it
    is no release at all -- and if the condition were ever loosened to accept a
    skip, it would be a release nobody tested.
    """
    jobs = workflow()["jobs"]
    for name in ("test", "test-3_12"):
        assert reaches(jobs[name], DISPATCH, MAIN, level=level), (
            f"the suite ({name}) would be skipped on a release dispatch"
        )
    assert reaches(
        jobs["release"], DISPATCH, MAIN, level=level, results=SUITE_GREEN_BENCHMARK_SKIPPED
    ), "the release job would not run on a release dispatch even with a green suite"


@pytest.mark.gitea_checkout
@pytest.mark.parametrize("level", ["patch", "minor"])
def test_a_release_dispatch_with_a_failed_suite_releases_nothing(level: str):
    jobs = workflow()["jobs"]
    assert not reaches(jobs["release"], DISPATCH, MAIN, level=level, results=SUITE_FAILED), (
        "a failing suite would still publish on a release dispatch"
    )


@pytest.mark.gitea_checkout
@pytest.mark.parametrize("level", ["patch", "minor"])
def test_a_release_dispatch_with_the_suite_failed_on_3_12_releases_nothing(level: str):
    jobs = workflow()["jobs"]
    assert not reaches(
        jobs["release"], DISPATCH, MAIN, level=level, results=SUITE_FAILED_ON_3_12
    ), "a suite failing on Python 3.12 alone would still publish"


@pytest.mark.gitea_checkout
def test_a_push_with_the_suite_failed_on_3_12_runs_no_release():
    assert not reaches(workflow()["jobs"]["release"], "push", MAIN, results=SUITE_FAILED_ON_3_12), (
        "a suite failing on Python 3.12 alone would still reach the release job on a push"
    )


@pytest.mark.gitea_checkout
def test_a_push_with_the_suite_failed_on_3_12_runs_no_benchmark():
    """The benchmark waits for both suites: a run on 3.12 beside it would skew the measurement."""
    job = workflow()["jobs"]["benchmark"]
    assert not reaches(job, "push", MAIN, results=SUITE_FAILED_ON_3_12)
    assert reaches(job, "push", MAIN, results=SUITE_GREEN_BENCHMARK_GREEN)


@pytest.mark.gitea_checkout
def test_a_release_dispatch_off_main_releases_nothing():
    """The ref gate holds for a dispatch as it does for a push."""
    assert not reaches(
        workflow()["jobs"]["release"],
        DISPATCH,
        "refs/heads/release/sprint-1",
        level="patch",
        results=SUITE_GREEN_BENCHMARK_SKIPPED,
    )


@pytest.mark.gitea_checkout
def test_the_benchmark_job_serialises_against_every_other_run():
    """Constant, for the fast-forward case where one commit arrives under two
    events and the ref-keyed workflow group does not collide them."""
    job = workflow()["jobs"]["benchmark"]
    assert serialises_host_wide(job), f"found concurrency: {job.get('concurrency')!r}"


@pytest.mark.gitea_checkout
def test_a_running_benchmark_is_queued_behind_not_cancelled():
    concurrency = workflow()["jobs"]["benchmark"]["concurrency"]
    assert concurrency.get("cancel-in-progress") is False, (
        f"cancel-in-progress must be false, found {concurrency.get('cancel-in-progress')!r}"
    )


def test_CONTROL_the_evaluator_distinguishes_the_conditions():
    """`reaches` must not be a function that says yes to everything."""
    dispatch_only = {"if": "github.event_name == 'workflow_dispatch'"}
    assert reaches(dispatch_only, DISPATCH, MAIN, results=DISPATCH_RESULTS)
    assert not reaches(dispatch_only, "push", MAIN, results=DISPATCH_RESULTS)
    assert reaches({}, "push", MAIN, results=DISPATCH_RESULTS), (
        "a job with no condition runs for every event"
    )


def test_CONTROL_a_ref_keyed_group_does_not_count():
    assert not serialises_host_wide(
        {"concurrency": {"group": "cliffracer-release-${{ github.ref }}"}}
    )
    assert not serialises_host_wide({})
    assert not serialises_host_wide({"concurrency": {}})


@pytest.mark.gitea_checkout
def test_the_guard_reads_the_real_workflow():
    """A parse that found no benchmark job would pass the checks above."""
    jobs = workflow()["jobs"]
    assert "benchmark" in jobs, "no benchmark job in the workflow this guard reads"
    assert jobs["benchmark"].get("runs-on") == "bench-tier"

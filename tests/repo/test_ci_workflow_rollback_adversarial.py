"""Adversarial tests for the release job's tag-rollback wiring, on every platform.

The same invariants are asserted against every workflow file in the repository,
Gitea's and GitHub's alike, because both are real: CI runs here and on the
upstream this tree is merged into. A guard that opened one file would be green
about the other.

Validates, per workflow:
1. Structural integrity of the release job, keyed on what each step runs rather
   than on the prose in its name.
2. Every ``steps.<id>`` the release job reads resolves to a step id the job
   declares, so renaming an id cannot silently blank out the references to it.
3. The step that decides a release writes both of the outputs the rest of the
   job reads back.
4. The rollback condition -- read out of the workflow, not retyped here --
   evaluates true on exactly the scenarios that warrant deleting the tag.

``evaluate_actions_if_expression`` below is a local model of Actions semantics,
not the runner, and nothing here proves the two agree. Its value is that it is
driven by the condition each workflow actually carries, so a semantic change to
one reaches these assertions instead of only a string comparison.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests.repo.ci_workflows import (
    PLATFORM_DIRS,
    ci_workflow_ids,
    ci_workflow_paths,
    load,
    rel,
)

pytestmark = pytest.mark.repo

# Matches a `steps.<id>.` context lookup in an `if:`, `run:` or `env:` value.
STEP_REFERENCE = re.compile(r"steps\.([A-Za-z0-9_-]+)\.")

# CI pipelines only: the rollback structure below is a property of the
# release pipeline, and a scheduled workflow has no part in it.
WORKFLOWS = ci_workflow_paths()
WORKFLOW_IDS = ci_workflow_ids()


def release_job(path: Path) -> dict[str, Any]:
    """Return the parsed `release` job of a workflow."""
    jobs = load(path).get("jobs", {})
    assert "release" in jobs, f"release job missing from {rel(path)}"
    return jobs["release"]


def step_running(steps: list[dict[str, Any]], fragment: str) -> int:
    """Return the index of the one step whose run script contains `fragment`.

    Steps are located by what they execute so that renaming a step for clarity
    does not redden the ordering assertions, and renaming one to disguise a
    behaviour change does not hide it.
    """
    matches = [i for i, s in enumerate(steps) if fragment in str(s.get("run", ""))]
    assert len(matches) == 1, (
        f"expected exactly one release step running {fragment!r}, found {len(matches)}: "
        f"{[steps[i].get('name', '<unnamed>') for i in matches]}"
    )
    return matches[0]


def referenced_step_ids(job: dict[str, Any]) -> set[str]:
    """Return every step id the job reads through a `steps.<id>.` lookup."""
    found: set[str] = set()
    for step in job.get("steps", []):
        scalars = [str(step.get("if", "")), str(step.get("run", ""))]
        scalars += [str(v) for v in (step.get("env") or {}).values()]
        for text in scalars:
            found.update(STEP_REFERENCE.findall(text))
    return found


def declared_step_ids(job: dict[str, Any]) -> set[str]:
    """Return every step id the job declares."""
    return {s["id"] for s in job.get("steps", []) if s.get("id")}


def evaluate_actions_if_expression(
    expr: str,
    job_status: str,
    step_outputs: dict[str, dict[str, str]],
    step_outcomes: dict[str, str],
) -> bool:
    """Simulate GitHub / Gitea Actions 'if' expression parser.

    Implements:
    - Status check function presence check: if no status check function
      is present, implicitly prepend 'success() &&'.
    - Status check functions: success(), failure(), always(), cancelled().
    - Context lookups: steps.<id>.outputs.<key>, steps.<id>.outcome.
    - Equality and boolean AND operators.
    """
    # Check for status check functions
    has_status_func = any(
        fn in expr for fn in ("success()", "failure()", "always()", "cancelled()")
    )
    effective_expr = expr if has_status_func else f"success() && ({expr})"

    # Context values
    def evaluate_atom(token: str) -> Any:
        token = token.strip()
        if token == "success()":
            return job_status == "success"
        if token == "failure()":
            return job_status == "failure"
        if token == "always()":
            return True
        if token == "cancelled()":
            return job_status == "cancelled"
        if token == "'true'":
            return "true"
        if token == "'false'":
            return "false"
        if token == "'success'":
            return "success"
        if token == "'failure'":
            return "failure"
        if token.startswith("steps.") and ".outputs." in token:
            parts = token.split(".")
            step_id = parts[1]
            out_name = parts[3]
            return step_outputs.get(step_id, {}).get(out_name, "")
        if token.startswith("steps.") and ".outcome" in token:
            parts = token.split(".")
            step_id = parts[1]
            return step_outcomes.get(step_id, "")
        return token

    # Simple evaluator for expressions of form: "atom1 && atom2 == atom3" or "atom1 == atom2 && atom3 == atom4"
    # Break by '&&'
    and_clauses = [c.strip() for c in effective_expr.split("&&")]
    results = []
    for clause in and_clauses:
        # Strip outer parens
        if clause.startswith("(") and clause.endswith(")"):
            clause = clause[1:-1].strip()
        if "==" in clause:
            lhs, rhs = clause.split("==")
            results.append(evaluate_atom(lhs) == evaluate_atom(rhs))
        elif "!=" in clause:
            lhs, rhs = clause.split("!=")
            results.append(evaluate_atom(lhs) != evaluate_atom(rhs))
        else:
            val = evaluate_atom(clause)
            results.append(bool(val))

    return all(results)


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_ci_workflow_yaml_syntax_and_rollback_step_structure(platform: str, path: Path) -> None:
    """Verify the workflow parses and its release steps run in the right order."""
    job = release_job(path)
    steps = job.get("steps", [])

    push_idx = step_running(steps, 'git push origin "$TAG"')
    build_idx = step_running(steps, "uv build --all-packages")
    pub_idx = step_running(steps, "uv publish dist/*")
    rollback_idx = step_running(steps, 'git push --delete origin "$TAG"')
    relnote_idx = step_running(steps, "scripts/release_note.py")

    assert push_idx < build_idx < pub_idx < rollback_idx < relnote_idx, (
        f"in {rel(path)} the tag must be pushed before anything is built or "
        "published, and the rollback must come after the publish it undoes"
    )

    rollback_step = steps[rollback_idx]
    assert rollback_step["if"] == "failure() && steps.decide.outputs.released == 'true'"

    run_script = str(rollback_step.get("run", ""))
    assert 'git push --delete origin "$TAG" || true' in run_script
    assert 'TAG="${{ steps.decide.outputs.tag }}"' in run_script


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_every_step_reference_resolves_to_a_declared_step_id(platform: str, path: Path) -> None:
    """Every `steps.<id>` the release job reads is an id the job declares.

    An unresolved reference evaluates to the empty string rather than erroring,
    so a renamed id turns every step gated on it into a permanent no-op.
    """
    job = release_job(path)
    referenced = referenced_step_ids(job)
    declared = declared_step_ids(job)

    assert referenced, f"the release job in {rel(path)} reads no step outputs at all"
    unresolved = referenced - declared
    assert not unresolved, (
        f"{rel(path)}: release steps read {sorted(unresolved)} but the job declares "
        f"only {sorted(declared)}; unresolved lookups silently evaluate to ''"
    )


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_the_decide_step_writes_the_outputs_the_job_reads_back(platform: str, path: Path) -> None:
    """The `decide` step writes both outputs the rest of the release job gates on."""
    job = release_job(path)
    decide = [s for s in job.get("steps", []) if s.get("id") == "decide"]
    assert len(decide) == 1, (
        f"{rel(path)}: the release job declares no single step with id 'decide'"
    )

    script = str(decide[0].get("run", ""))
    for output in ("released", "tag"):
        assert f'echo "{output}=' in script, (
            f"{rel(path)}: the decide step never writes {output!r}, yet other "
            f"steps read steps.decide.outputs.{output}"
        )
    assert '>> "$GITHUB_OUTPUT"' in script, (
        f"{rel(path)}: the decide step writes no value into $GITHUB_OUTPUT"
    )


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_the_rollback_condition_in_the_workflow_fires_only_when_it_should(
    platform: str, path: Path
) -> None:
    """Evaluate each workflow's own rollback condition across release outcomes."""
    job = release_job(path)
    steps = job.get("steps", [])
    rollback_expr = steps[step_running(steps, 'git push --delete origin "$TAG"')]["if"]

    # Publish fails after the tag was pushed: the tag must come back off.
    assert (
        evaluate_actions_if_expression(
            rollback_expr,
            job_status="failure",
            step_outputs={"decide": {"released": "true", "tag": "v5.4.0"}},
            step_outcomes={"publish": "failure"},
        )
        is True
    )

    # Build fails after the tag was pushed, so publish never ran at all.
    assert (
        evaluate_actions_if_expression(
            rollback_expr,
            job_status="failure",
            step_outputs={"decide": {"released": "true", "tag": "v5.4.0"}},
            step_outcomes={"publish": ""},
        )
        is True
    )

    # Happy path: the tag stays.
    assert (
        evaluate_actions_if_expression(
            rollback_expr,
            job_status="success",
            step_outputs={"decide": {"released": "true", "tag": "v5.4.0"}},
            step_outcomes={"publish": "success"},
        )
        is False
    )

    # No release warranted, so there is no tag to remove.
    assert (
        evaluate_actions_if_expression(
            rollback_expr,
            job_status="success",
            step_outputs={"decide": {"released": "false"}},
            step_outcomes={},
        )
        is False
    )

    # Failure before the tag was pushed: nothing to roll back.
    assert (
        evaluate_actions_if_expression(
            rollback_expr,
            job_status="failure",
            step_outputs={"decide": {}},
            step_outcomes={},
        )
        is False
    )


def test_CONTROL_the_scenario_table_rejects_a_condition_that_misses_build_failures() -> None:
    """The scenarios above discriminate: a plausible wrong condition fails them.

    Without this, a rollback condition that never fires would satisfy every
    `is False` assertion above and only the two `is True` cases would carry the
    test.
    """
    buggy_expr = "steps.publish.outcome == 'failure'"

    # It does fire when publish itself failed ...
    assert (
        evaluate_actions_if_expression(
            buggy_expr,
            job_status="failure",
            step_outputs={"decide": {"released": "true"}},
            step_outcomes={"publish": "failure"},
        )
        is False
    )

    # ... and it misses a build failure, where the tag is already pushed.
    assert (
        evaluate_actions_if_expression(
            buggy_expr,
            job_status="failure",
            step_outputs={"decide": {"released": "true"}},
            step_outcomes={"publish": ""},
        )
        is False
    )


@pytest.mark.gitea_checkout
def test_every_platform_directory_contributes_a_workflow() -> None:
    """Each platform has at least one workflow, so none can drop out unnoticed.

    Parametrizing over discovered files means an empty directory produces zero
    cases, and zero cases is a green run that asserted nothing.
    """
    assert WORKFLOWS, "no CI pipeline was discovered at all"
    for platform, directory in PLATFORM_DIRS.items():
        found = [p for plat, p in WORKFLOWS if plat == platform]
        assert found, (
            f"{platform} contributes no CI pipeline from {directory}. A directory "
            f"holding only non-CI workflows would produce zero cases here, which is "
            f"a green run that asserted nothing."
        )

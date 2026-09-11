"""Adversarial tests for Gitea / GitHub Actions CI workflow rollback syntax and logic.

Empirically validates:
1. Structural integrity of .gitea/workflows/ci.yml release job.
2. Exact expression syntax of the release rollback step.
3. Evaluation of Actions expression semantics across all failure and success scenarios.
4. Correct ordering and error resilience (|| true) of the tag deletion command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".gitea" / "workflows" / "ci.yml"


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


@pytest.mark.unit
def test_ci_workflow_yaml_syntax_and_rollback_step_structure() -> None:
    """Verify that ci.yml parses cleanly and rollback step has exact configuration."""
    assert CI_WORKFLOW.exists(), f"{CI_WORKFLOW} does not exist"
    data = yaml.safe_load(CI_WORKFLOW.read_text())

    jobs = data.get("jobs", {})
    assert "release" in jobs, "release job missing from ci.yml"
    release_job = jobs["release"]

    steps = release_job.get("steps", [])
    step_names = [s.get("name", "") for s in steps]

    assert "Push the tag FIRST, before anything is built or published" in step_names
    assert "Build" in step_names
    assert "Publish to the private Gitea PyPI registry" in step_names
    assert "Delete the tag if publishing failed" in step_names
    assert "Write the changelog into a Gitea release note" in step_names

    # Check order: Push tag -> Build -> Publish -> Rollback -> Relnote
    push_idx = step_names.index("Push the tag FIRST, before anything is built or published")
    build_idx = step_names.index("Build")
    pub_idx = step_names.index("Publish to the private Gitea PyPI registry")
    rollback_idx = step_names.index("Delete the tag if publishing failed")
    relnote_idx = step_names.index("Write the changelog into a Gitea release note")

    assert push_idx < build_idx < pub_idx < rollback_idx < relnote_idx

    rollback_step = steps[rollback_idx]
    # Verify exact if condition
    assert rollback_step["if"] == "failure() && steps.decide.outputs.released == 'true'"

    # Verify rollback run script
    run_script = rollback_step.get("run", "")
    assert 'git push --delete origin "$TAG" || true' in run_script
    assert 'TAG="${{ steps.decide.outputs.tag }}"' in run_script


@pytest.mark.unit
def test_actions_expression_evaluates_correctly_under_all_scenarios() -> None:
    """Empirically verify evaluation across all build/publish outcomes."""
    rollback_expr = "failure() && steps.decide.outputs.released == 'true'"
    buggy_expr = "steps.publish.outcome == 'failure'"

    # Scenario 1: Publish fails after tag pushed
    # Job status: failure. released: 'true'.
    assert (
        evaluate_actions_if_expression(
            rollback_expr,
            job_status="failure",
            step_outputs={"decide": {"released": "true", "tag": "v5.4.0"}},
            step_outcomes={"publish": "failure"},
        )
        is True
    )

    # Confirm old buggy expression would have evaluated to False (skipped)
    assert (
        evaluate_actions_if_expression(
            buggy_expr,
            job_status="failure",
            step_outputs={"decide": {"released": "true"}},
            step_outcomes={"publish": "failure"},
        )
        is False
    )

    # Scenario 2: Build fails after tag pushed (before publish runs)
    # Job status: failure. released: 'true'. publish outcome: empty/skipped.
    assert (
        evaluate_actions_if_expression(
            rollback_expr,
            job_status="failure",
            step_outputs={"decide": {"released": "true", "tag": "v5.4.0"}},
            step_outcomes={"publish": ""},
        )
        is True
    )

    # Buggy expression would fail to roll back build failures
    assert (
        evaluate_actions_if_expression(
            buggy_expr,
            job_status="failure",
            step_outputs={"decide": {"released": "true"}},
            step_outcomes={"publish": ""},
        )
        is False
    )

    # Scenario 3: Happy path (everything succeeds)
    # Job status: success. released: 'true'.
    assert (
        evaluate_actions_if_expression(
            rollback_expr,
            job_status="success",
            step_outputs={"decide": {"released": "true", "tag": "v5.4.0"}},
            step_outcomes={"publish": "success"},
        )
        is False
    )

    # Scenario 4: No release warranted
    # Job status: success. released: 'false'.
    assert (
        evaluate_actions_if_expression(
            rollback_expr,
            job_status="success",
            step_outputs={"decide": {"released": "false"}},
            step_outcomes={},
        )
        is False
    )

    # Scenario 5: Early failure before tag pushed (e.g. decide failed)
    # Job status: failure. released: not set or 'false'.
    assert (
        evaluate_actions_if_expression(
            rollback_expr,
            job_status="failure",
            step_outputs={"decide": {}},
            step_outcomes={},
        )
        is False
    )

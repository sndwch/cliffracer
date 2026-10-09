"""A release step reports a broken tool as a failure, not as silence.

Both pipelines decide whether to release by running `semantic-release` and
reading what it prints. Both used to run it as

    NEXT="$(uv run semantic-release ... --print-tag 2>/dev/null || true)"

and then treat an empty `NEXT` as "no release warranted". Those two words cover
two different worlds: the tool ran and found nothing to bump, or the tool did
not run at all. `2>/dev/null` threw away the reason and `|| true` threw away
the exit status, so both produced an empty `NEXT`, printed the same sentence,
and left the job green.

That is not hypothetical. Releases stopped after `v1.0.0-rc.4` and nothing said
so for 463 commits, because "no release warranted" is exactly what success
looks like. The distinction is available for free: a real computation exits 0
and prints a tag, a failure exits non-zero and prints nothing.

The second rule here is about the level. The Gitea pipeline cuts a prerelease
from a `workflow_dispatch` input, and that input must not offer `major`. The
level is a person's decision, and this repository's standing rule is that a
break may ship in a minor or a patch while the major number must not climb
quickly -- so the one level the workflow must not be able to reach is the one
that climbs it.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.repo.ci_workflows import (
    ci_workflow_paths,
    load,
    rel,
)

pytestmark = pytest.mark.repo

WORKFLOWS = ci_workflow_paths()
WORKFLOW_IDS = [f"{platform}:{path.name}" for platform, path in WORKFLOWS]

# Read off the parsed step list rather than the file's text, so a line in a
# comment is not mistaken for a command. A comment in this repository may
# legitimately quote the shape it is warning against -- the release job's own
# comment does exactly that -- and a text scan would red on the explanation.
SWALLOWED = ("2>/dev/null", "|| true")

# The level this workflow must not be able to choose.
FORBIDDEN_LEVEL = "major"

# The step that decides, and the output every later step reads.
DECISION_STEP = "decide"
DECISION = "steps.decide.outputs.released"


def release_commands(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(job name, run block) for every step that runs semantic-release."""
    found = []
    for name, job in (data.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            script = step.get("run")
            if isinstance(script, str) and "semantic-release" in script:
                found.append((name, script))
    return found


def swallowed_failures(data: dict[str, Any]) -> list[str]:
    """Lines invoking semantic-release whose failure cannot be seen."""
    hidden = []
    for job, script in release_commands(data):
        for line in script.splitlines():
            if "semantic-release" not in line:
                continue
            for pattern in SWALLOWED:
                if pattern in line:
                    hidden.append(f"{job}: {pattern} in {line.strip()}")
    return hidden


def dispatch_levels(data: dict[str, Any]) -> list[str]:
    """The options of a `level` dispatch input, or [] when there is none."""
    raw = data.get(True, data.get("on"))
    if not isinstance(raw, dict):
        return []
    dispatch = raw.get("workflow_dispatch")
    if not isinstance(dispatch, dict):
        return []
    level = (dispatch.get("inputs") or {}).get("level")
    if not isinstance(level, dict):
        return []
    return [str(option) for option in level.get("options") or []]


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_a_release_step_never_swallows_the_tools_failure(platform: str, path: Path):
    hidden = swallowed_failures(load(path))
    assert hidden == [], (
        f"{rel(path)} hides whether semantic-release ran at all. A tool that "
        f"failed then reads as a tool that found nothing to release, and the "
        f"job goes green either way:\n  " + "\n  ".join(hidden)
    )


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_a_release_level_input_cannot_choose_major(platform: str, path: Path):
    levels = dispatch_levels(load(path))
    assert FORBIDDEN_LEVEL not in levels, (
        f"{rel(path)} offers '{FORBIDDEN_LEVEL}' as a release level. A break "
        f"may ship in a minor or a patch here; the major number climbing is "
        f"the thing to avoid, so it is not a button this workflow has. "
        f"Options found: {levels}"
    )


def test_at_least_one_pipeline_runs_the_release_tool():
    """Otherwise both tests above pass by finding nothing to examine.

    The failure mode of a scan is silence: rename the step, or move the
    release to a workflow these helpers do not return, and every assertion
    here holds vacuously while nothing is checked. This is the assertion that
    such a change reds.
    """
    runners = [
        f"{platform}:{path.name}" for platform, path in WORKFLOWS if release_commands(load(path))
    ]
    assert runners, (
        "No CI pipeline runs semantic-release, so the release guards in this "
        "file examined nothing. Either the release moved somewhere these "
        "helpers do not look, or the step was renamed."
    )


def ungated_after_the_decision(data: dict[str, Any]) -> list[str]:
    """Steps that run after the decision without reading it.

    Anything following the decision is a side effect -- a tag, a build, an
    upload, a release note -- and the decision is the only thing standing
    between a run that merely computed a version and a run that published one.
    """
    ungated = []
    for name, job in (data.get("jobs") or {}).items():
        decided = False
        for step in job.get("steps") or []:
            if decided and DECISION not in str(step.get("if") or ""):
                ungated.append(f"{name}: {step.get('name') or step.get('uses')}")
            if step.get("id") == DECISION_STEP:
                decided = True
    return ungated


@pytest.mark.parametrize(("platform", "path"), WORKFLOWS, ids=WORKFLOW_IDS)
def test_nothing_is_published_without_the_decision_saying_so(platform: str, path: Path):
    """The Gitea pipeline runs this job on a push to main without releasing.

    A push computes the version and stops; a dispatch carrying a level
    publishes. What separates them is not the job's `if:` -- the job runs
    either way -- but this output, read by every step that has an effect
    outside the runner. A step added after the decision and not gated on it
    publishes on every push to main, which is the one thing this shape must
    not do quietly.
    """
    ungated = ungated_after_the_decision(load(path))
    assert ungated == [], (
        f"{rel(path)} has steps after the release decision that do not read "
        f"it, so they would run whether or not a release was decided:\n  " + "\n  ".join(ungated)
    )


def test_CONTROL_an_ungated_publishing_step_is_reported(tmp_path: Path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "on:\n  push:\n    branches: [ main ]\n"
        "jobs:\n"
        "  release:\n"
        "    steps:\n"
        "      - name: Decide\n"
        "        id: decide\n"
        "        run: echo semantic-release\n"
        "      - name: Build\n"
        f"        if: {DECISION} == 'true'\n"
        "        run: uv build\n"
        "      - name: Publish\n"
        "        run: uv publish\n"
    )

    assert ungated_after_the_decision(yaml.safe_load(workflow.read_text())) == ["release: Publish"]


def test_CONTROL_a_swallowed_failure_is_reported(tmp_path: Path):
    """The shape this file exists to reject, read back through the checker."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "on:\n  push:\n    branches: [ main ]\n"
        "jobs:\n"
        "  release:\n"
        "    steps:\n"
        "      - name: Decide\n"
        "        run: |\n"
        '          NEXT="$(uv run semantic-release version --print-tag 2>/dev/null || true)"\n'
    )
    hidden = swallowed_failures(yaml.safe_load(workflow.read_text()))

    assert len(hidden) == 2, hidden
    assert any("2>/dev/null" in entry for entry in hidden)
    assert any("|| true" in entry for entry in hidden)


def test_CONTROL_a_clean_release_command_is_not_reported(tmp_path: Path):
    """A near miss: the swallowing patterns present, but not on that line.

    `git describe ... 2>/dev/null || true` is legitimate and lives in the same
    step. A checker that scanned the block rather than the line would reject
    the correct file, and a guard that reds on correct code gets weakened.
    """
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "on:\n  push:\n    branches: [ main ]\n"
        "jobs:\n"
        "  release:\n"
        "    steps:\n"
        "      - name: Decide\n"
        "        run: |\n"
        '          NEXT="$(uv run semantic-release version --print-tag)"\n'
        '          PREV="$(git describe --tags --abbrev=0 2>/dev/null || true)"\n'
    )

    assert swallowed_failures(yaml.safe_load(workflow.read_text())) == []


def test_CONTROL_a_major_level_option_is_reported(tmp_path: Path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "on:\n"
        "  push:\n    branches: [ main ]\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      level:\n"
        "        type: choice\n"
        "        options: [ none, patch, minor, major ]\n"
        "jobs: {}\n"
    )

    assert FORBIDDEN_LEVEL in dispatch_levels(yaml.safe_load(workflow.read_text()))


def test_CONTROL_a_workflow_without_the_input_reports_no_levels(tmp_path: Path):
    """No input is not the same as an input offering nothing.

    Both return `[]`, and the level assertion passes on both -- correctly, a
    workflow that cannot be dispatched cannot be dispatched at major. This
    pins that reading so the empty list is not later mistaken for a finding.
    """
    workflow = tmp_path / "ci.yml"
    workflow.write_text("on:\n  push:\n    branches: [ main ]\njobs: {}\n")

    assert dispatch_levels(yaml.safe_load(workflow.read_text())) == []

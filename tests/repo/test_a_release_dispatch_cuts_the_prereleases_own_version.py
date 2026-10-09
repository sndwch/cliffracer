"""`level: release` cuts the version its prerelease leads to, whatever the commits say.

A `release` dispatch ran semantic-release with no bump flag, and semantic-release computes the
next version from every commit since the last FINAL release. Two conventional `BREAKING CHANGE:`
commits sat in that range, so it computed a major from `v1.1.0-rc.1`, and the job tagged and
published `v2.0.0`. No flag brings `v1.1.0` back: `--patch` computes `v1.1.1` and `--minor`
`v1.2.0`. So the tag is read from the prerelease by `scripts/release_target.py`, and the decision
step does not ask the tool for it.

The decision step is run for real, in a temporary repository, with the real tool installed for
the control. The commits are conventional on purpose: this repository's own are prose, which the
parser cannot read and so never reaches their footers, and that is what hid the hazard.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "release_target.py"
BREAKING = "feat: change a thing\n\nBREAKING CHANGE: callers must change."


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, message: str) -> None:
    (repo / "history.txt").write_text(f"{git(repo, 'rev-list', '--count', '--all')}: {message}\n")
    git(repo, "add", "history.txt")
    git(repo, "commit", "-m", message)


@pytest.fixture
def incident(tmp_path: Path) -> Path:
    """v1.0.0, two breaking conventional commits, v1.1.0-rc.1, then one more commit."""
    repo = tmp_path
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Release check")
    git(repo, "config", "user.email", "release@example.invalid")
    git(repo, "remote", "add", "origin", "https://forge.example.invalid/warehouse/orders.git")
    (repo / "pyproject.toml").write_bytes((REPO / "pyproject.toml").read_bytes())
    (repo / "scripts").symlink_to(REPO / "scripts")
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "-m", "chore: the baseline")
    git(repo, "tag", "v1.0.0")
    commit(repo, BREAKING)
    commit(repo, BREAKING.replace("change a thing", "change another"))
    git(repo, "tag", "v1.1.0-rc.1")
    commit(repo, "docs: after the prerelease")
    return repo


def run_decision(repo: Path):
    workflow = yaml.safe_load((REPO / ".gitea/workflows/ci.yml").read_text())
    step = next(step for step in workflow["jobs"]["release"]["steps"] if step.get("id") == "decide")
    output = repo / "decision.txt"
    output.write_text("")
    env = dict(
        os.environ,
        LEVEL="release",
        GITHUB_OUTPUT=str(output),
        UV_PROJECT_ENVIRONMENT=sys.prefix,
        UV_NO_SYNC="true",
    )
    result = subprocess.run(
        ["bash", "-c", step["run"]], cwd=repo, env=env, capture_output=True, text=True, timeout=60
    )
    return result, output.read_text().splitlines()


#: What the workflow's decide step removes from the environment before it runs the tool. The runner
#: sets the forge URLs over plain http inside its network, which the tool refuses ("Insecure
#: connections are currently disabled"); the step runs the print-only commands without them.
STRIPPED = ("GITHUB_OUTPUT", "GITHUB_SERVER_URL", "GITHUB_API_URL")


def workflow_stripped_names() -> list[set[str]]:
    """The names each `env -u ... uv run semantic-release` in the decide step removes."""
    workflow = yaml.safe_load((REPO / ".gitea/workflows/ci.yml").read_text())
    step = next(step for step in workflow["jobs"]["release"]["steps"] if step.get("id") == "decide")
    return [
        set(re.findall(r"-u (\w+)", match))
        for match in re.findall(r"env((?: -u \w+)+) uv run semantic-release", step["run"])
    ]


def test_the_control_strips_the_variables_the_workflow_strips():
    """The control runs the tool the way the decide step does, and the two lists are one list."""
    found = workflow_stripped_names()

    assert found, "the decide step runs semantic-release through no `env -u ...` any more"
    assert all(names == set(STRIPPED) for names in found), (found, STRIPPED)


def test_CONTROL_the_tool_computes_a_major_for_this_history(incident: Path):
    """Without this, the test below could pass on a history the tool reads correctly.

    Run as CI runs it: the forge URLs are in the environment over http, and the command removes
    them with the workflow's own `env -u` list. Without that removal the tool refuses to run.
    """
    env = dict(
        os.environ,
        UV_PROJECT_ENVIRONMENT=sys.prefix,
        UV_NO_SYNC="true",
        GITHUB_OUTPUT=str(incident / "tool-output.txt"),
        GITHUB_SERVER_URL="http://forge.example.invalid",
        GITHUB_API_URL="http://forge.example.invalid/api/v1",
    )
    removals = [arg for name in STRIPPED for arg in ("-u", name)]
    computed = subprocess.run(
        [
            "env",
            *removals,
            "uv",
            "run",
            "semantic-release",
            "-c",
            "pyproject.toml",
            "version",
            "--print-tag",
        ],
        cwd=incident,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert computed.returncode == 0, computed.stdout + computed.stderr
    assert computed.stdout.strip() == "v2.0.0", computed.stdout


def test_a_release_dispatch_tags_the_base_version_of_its_prerelease(incident: Path):
    before = git(incident, "show-ref")

    result, outputs = run_decision(incident)

    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == ["released=true", "tag=v1.1.0"], (outputs, result.stdout)
    assert git(incident, "show-ref") == before, "deciding a version changed a ref"


def promoted(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo)], capture_output=True, text=True
    )


@pytest.mark.parametrize(
    ("tags", "rc", "final"),
    [
        (["v1.1.0-rc.1"], "v1.1.0-rc.1", "v1.1.0"),
        (["v1.1.0-rc.1", "v1.1.0-rc.2"], "v1.1.0-rc.2", "v1.1.0"),
        (["v1.1.0-rc.2", "v1.1.0-rc.10"], "v1.1.0-rc.10", "v1.1.0"),
        (["v1.1.0-rc.3", "v1.2.0-rc.1"], "v1.2.0-rc.1", "v1.2.0"),
        (["v1.0.0-rc.4", "v1.1.0-rc.1"], "v1.1.0-rc.1", "v1.1.0"),
        (["v1.9.1-rc.1", "v1.10.0-rc.1"], "v1.10.0-rc.1", "v1.10.0"),
        (["v1.2.9-rc.1", "v1.2.10-rc.1"], "v1.2.10-rc.1", "v1.2.10"),
        (["v9.0.0-rc.1", "v10.0.0-rc.1"], "v10.0.0-rc.1", "v10.0.0"),
    ],
    ids=[
        "one",
        "newest-rc",
        "numeric-not-textual",
        "newest-base",
        "earlier-base-ignored",
        "two-digit-minor",
        "two-digit-patch",
        "two-digit-major",
    ],
)
def test_the_newest_prerelease_names_the_final_tag(
    incident: Path, tags: list[str], rc: str, final: str
):
    git(incident, "tag", "-d", "v1.1.0-rc.1")
    for tag in tags:
        git(incident, "tag", tag, "HEAD~1")

    result = promoted(incident)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [rc, final], result.stdout


def refuse_without_a_prerelease(repo: Path) -> None:
    git(repo, "tag", "-d", "v1.1.0-rc.1")


def refuse_when_the_final_exists(repo: Path) -> None:
    git(repo, "tag", "v1.1.0", "HEAD")


def refuse_when_a_higher_final_exists(repo: Path) -> None:
    git(repo, "tag", "v2.0.0", "HEAD")


def refuse_when_the_prerelease_is_not_an_ancestor(repo: Path) -> None:
    git(repo, "tag", "-d", "v1.1.0-rc.1")
    git(repo, "switch", "-q", "-c", "side", "HEAD~2")
    commit(repo, "docs: a commit on a side branch")
    git(repo, "tag", "v1.1.0-rc.1")
    git(repo, "switch", "-q", "main")


@pytest.mark.parametrize(
    ("arrange", "says"),
    [
        (refuse_without_a_prerelease, "no prerelease tag"),
        (refuse_when_the_final_exists, "already tagged, so v1.1.0-rc.1 has nothing left"),
        (refuse_when_a_higher_final_exists, "v2.0.0 is already tagged and is higher"),
        (refuse_when_the_prerelease_is_not_an_ancestor, "not an ancestor of HEAD"),
    ],
    ids=["no-prerelease", "final-exists", "higher-final-exists", "not-an-ancestor"],
)
def test_the_script_refuses_what_it_cannot_promote_and_says_why(incident: Path, arrange, says: str):
    arrange(incident)
    before = git(incident, "show-ref", "--tags")

    result = promoted(incident)

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == ""
    assert says in result.stderr, result.stderr
    assert git(incident, "show-ref", "--tags") == before


@pytest.mark.parametrize(
    "arrange",
    [refuse_when_the_final_exists, refuse_when_a_higher_final_exists],
    ids=["final-exists", "higher-final-exists"],
)
def test_a_refusal_stops_the_decision_with_nothing_published(incident: Path, arrange):
    arrange(incident)
    before = git(incident, "show-ref")

    result, outputs = run_decision(incident)

    assert result.returncode != 0
    assert "THE RELEASE TARGET COULD NOT BE DECIDED" in result.stdout
    assert outputs == [], "a refused promotion still produced a decision"
    assert git(incident, "show-ref") == before, "a refused promotion changed a ref"


def merge_an_unrelated_branch_after_the_prerelease(repo: Path) -> None:
    """HEAD is the prerelease plus a merge of work that was never part of it."""
    git(repo, "switch", "-q", "-c", "unrelated", "v1.0.0")
    (repo / "unrelated.txt").write_text("unrelated work\n")
    git(repo, "add", "unrelated.txt")
    git(repo, "commit", "-m", "docs: unrelated work")
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "--no-ff", "-m", "Merge the unrelated work", "unrelated")


def test_a_prerelease_behind_head_by_an_unrelated_merge_is_still_promoted(incident: Path):
    merge_an_unrelated_branch_after_the_prerelease(incident)
    assert git(incident, "merge-base", "--is-ancestor", "v1.1.0-rc.1", "HEAD") == ""
    before = git(incident, "show-ref")

    result, outputs = run_decision(incident)

    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == ["released=true", "tag=v1.1.0"], (outputs, result.stdout)
    assert git(incident, "show-ref") == before


def test_a_prerelease_the_head_was_not_built_from_is_still_refused_after_such_a_merge(
    incident: Path,
):
    """The control for the test above: the merge, not a leniency, is what lets it through."""
    refuse_when_the_prerelease_is_not_an_ancestor(incident)
    merge_an_unrelated_branch_after_the_prerelease(incident)

    result = promoted(incident)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "not an ancestor of HEAD" in result.stderr


def test_a_two_digit_minor_is_newer_than_a_one_digit_patch_of_an_earlier_minor(incident: Path):
    git(incident, "tag", "-d", "v1.1.0-rc.1")
    git(incident, "tag", "v1.9.0", "HEAD~2")
    git(incident, "tag", "v1.9.1-rc.1", "HEAD~1")
    git(incident, "tag", "v1.10.0-rc.1", "HEAD~1")

    result = promoted(incident)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["v1.10.0-rc.1", "v1.10.0"], result.stdout


def test_a_final_with_a_two_digit_minor_is_higher_than_a_prerelease_of_an_earlier_minor(
    incident: Path,
):
    git(incident, "tag", "-d", "v1.1.0-rc.1")
    git(incident, "tag", "v1.10.0", "HEAD~2")
    git(incident, "tag", "v1.9.1-rc.1", "HEAD~1")

    result = promoted(incident)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "v1.10.0 is already tagged and is higher than v1.9.1" in result.stderr

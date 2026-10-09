"""A workflow that does not gate a change is not held to the pipeline rules.

The CI guards read every file in a platform's workflow directory as *the*
pipeline: one per platform, the same gates on both, a pinned runner for its test
job, a rollback step structure. A nightly soak or a dispatch-only maintenance
job satisfies none of that and should not: it has no test job, no gates, and no
counterpart on the other platform to match step for step. Adding one reddened
ten guards at once, none of which was describing a real problem.

THE DISTINCTION IS DERIVED FROM THE TRIGGERS. `ci_workflows.is_ci_pipeline`
asks whether `on:` carries `push` or `pull_request`, because that is what makes
a workflow gate a change. A list of exempt filenames would let a file opt out of
the pipeline rules by being renamed, and the file's name is not what decides.

The consequence is deliberate and is the control at the bottom of this file: add
a push trigger to a scheduled workflow and it becomes a CI pipeline, with
one-per-platform and every gate rule applying at once.

WHAT STILL APPLIES. Being exempt from the pipeline shape is not being exempt.
`tests/repo/test_workflows_carry_no_history.py` scans every workflow file
through `workflow_paths()`, CI or not -- what a workflow is for does not change
what it may not contain -- and the rules below are this file's own.

THE THREE PARAMETRIZED RULES HAVE NO SUBJECT UNTIL A SCHEDULED WORKFLOW LANDS.
On a tree with only the two ci.yml files they collect zero cases, which pytest
reports as a skip: visible, but not an assertion. So each of them also has a
control that exercises the same helper against a fixture, and those run on every
tree. A rule whose only subject is a file that does not exist yet is a rule
nobody has tested.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from tests.repo.ci_workflows import (
    CI_TRIGGERS,
    PLATFORM_DIRS,
    ci_workflow_files,
    is_ci_pipeline,
    load,
    non_ci_workflow_ids,
    non_ci_workflow_paths,
    rel,
    trigger_names,
    workflow_paths,
)
from tests.repo.test_workflows_carry_no_history import (
    history_in_workflows,
    private_term_hits,
)

pytestmark = pytest.mark.repo

NON_CI = non_ci_workflow_paths()
NON_CI_IDS = non_ci_workflow_ids()

# `docker run`'s image is the first positional argument, which a regex cannot
# pick out: the first plausible-looking token on the line is usually the value of
# `--name`, and `${{ github.run_id }}` tokenises into three words. A first
# attempt at this matched `--name`'s value and would have flagged a correctly
# pinned image, so the arguments are walked instead.
TEMPLATE = re.compile(r"\$\{\{[^}]*\}\}")

# Options that take a separate value, so the value is not mistaken for the image.
FLAGS_WITH_VALUE = frozenset(
    {
        "-p",
        "--publish",
        "--name",
        "-e",
        "--env",
        "--env-file",
        "-v",
        "--volume",
        "--network",
        "--net",
        "-w",
        "--workdir",
        "-l",
        "--label",
        "-u",
        "--user",
        "--entrypoint",
        "--health-cmd",
        "--add-host",
        "--cpus",
        "-m",
        "--memory",
        "--restart",
        "--log-driver",
        "--mount",
        "--device",
        "--ulimit",
        "--platform",
    }
)


def docker_images(script: str) -> list[str]:
    """Every image a `docker run` in this script starts.

    Templated expressions are collapsed first -- `${{ github.run_id }}` is one
    value that happens to contain spaces -- and then the argument list is walked,
    skipping options and the values of options that take one. The image is the
    first positional argument.

    An image that is itself templated is returned as-is; the caller cannot judge
    whether it is pinned, and saying so is better than guessing either way.
    """
    found: list[str] = []
    for raw in script.replace("\\\n", " ").splitlines():
        line = TEMPLATE.sub("TEMPLATE", raw)
        if not re.search(r"\bdocker\s+run\b", line):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:  # pragma: no cover - unbalanced quotes fail elsewhere
            tokens = line.split()
        if "docker" not in tokens:
            continue
        index = tokens.index("docker")
        if index + 1 >= len(tokens) or tokens[index + 1] != "run":
            continue
        index += 2
        while index < len(tokens):
            token = tokens[index]
            if token in FLAGS_WITH_VALUE:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            found.append(token)
            break
    return found


def unpinned(images: list[str]) -> list[str]:
    """The images with no fixed version: a bare name, or `:latest`."""
    return [i for i in images if "TEMPLATE" not in i and (":" not in i or i.endswith(":latest"))]


def concurrency_group(workflow_data: dict) -> str | None:
    """The workflow's concurrency group, however it is spelled.

    `concurrency:` takes either a bare string or a mapping with a `group` key.
    """
    concurrency = workflow_data.get("concurrency")
    if isinstance(concurrency, dict):
        return concurrency.get("group")
    return concurrency


def _run_scripts(workflow_data: dict) -> list[str]:
    scripts = []
    for job in workflow_data.get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                scripts.append(step["run"])
    return scripts


# --- the rules a non-CI workflow answers to ----------------------------------


@pytest.mark.parametrize(("platform", "path"), NON_CI, ids=NON_CI_IDS)
def test_it_carries_no_ci_trigger(platform: str, path: Path):
    """Definitional, and asserted so the classification cannot quietly invert.

    If this ever fails, the file is a CI pipeline and belongs to the pipeline
    guards -- which is the right outcome, not something to exempt here.
    """
    found = trigger_names(load(path)) & CI_TRIGGERS

    assert not found, (
        f"{rel(path)} triggers on {sorted(found)}, so it gates a change and is a CI "
        f"pipeline. It is being read as a non-CI workflow, which means the pipeline "
        f"rules are not being applied to it. Move it to the CI shape or drop the "
        f"trigger."
    )


@pytest.mark.parametrize(("platform", "path"), NON_CI, ids=NON_CI_IDS)
def test_it_declares_a_concurrency_group(platform: str, path: Path):
    """Two overlapping runs of a soak share this host's broker and ports.

    A CI pipeline gets this from the file-level group the platform's ci.yml
    declares; a workflow outside that file has to say it itself.
    """
    group = concurrency_group(load(path))

    assert group, (
        f"{rel(path)} declares no concurrency group, so two runs can overlap on one "
        f"host and contend for the broker, the ports and the runner."
    )


@pytest.mark.parametrize(("platform", "path"), NON_CI, ids=NON_CI_IDS)
def test_every_container_it_starts_names_a_fixed_image(platform: str, path: Path):
    """An unpinned image makes a long run unreproducible and a failure unreadable.

    The same rule the CI pipelines follow for their broker container, stated
    here because these workflows start their own.
    """
    loose = []
    for script in _run_scripts(load(path)):
        loose.extend(unpinned(docker_images(script)))

    assert not loose, (
        f"{rel(path)} starts {loose} without a fixed version. Pin the tag, so a run "
        f"months from now is the same run and a failure names a known image."
    )


# --- what exemption does not cover -------------------------------------------


def test_the_history_scan_still_reads_every_non_ci_workflow():
    """Exempt from the pipeline shape is not exempt from the content rules.

    `test_workflows_carry_no_history.py` derives its targets from
    `workflow_paths()`, which is every file. Asserted here rather than trusted,
    because narrowing that helper to the CI pipelines is exactly the change
    someone would make while extending this file -- and it would take a
    scheduled workflow out of the private-term scan without anything going red.
    """
    scanned = {path for _, path in workflow_paths()}
    missing = [rel(path) for _, path in NON_CI if path not in scanned]

    assert not missing, f"these workflows are outside the history and private-term scan: {missing}"


def test_a_schedule_only_workflow_is_still_scanned_for_history(tmp_path: Path):
    """Membership is not enough: the scan must actually report on such a file.

    So this hands the scanner a schedule-only workflow carrying a comment of the
    kind it exists to reject, and requires it to be named. A version of this
    that only compared path lists would pass while the scan quietly skipped it.
    """
    planted = _write(
        tmp_path,
        "nightly.yml",
        SCHEDULE_ONLY.replace("name: Nightly", "# Closes #1234\nname: Nightly"),
    )

    clean = _write(tmp_path, "clean.yml", SCHEDULE_ONLY)

    assert history_in_workflows([clean]) == [], (
        "the scan reports on a clean schedule-only file, so the assertion below "
        "would pass for the wrong reason"
    )

    reported = history_in_workflows([planted])

    assert reported, (
        "the history scan reported nothing for a schedule-only workflow carrying "
        "an issue reference in a comment"
    )
    assert any("1234" in line for line in reported), reported


def test_a_schedule_only_workflow_is_still_scanned_for_private_terms(tmp_path: Path, monkeypatch):
    """The same for the terms this repository does not publish.

    The term is set on the environment the scanner reads, rather than passed,
    so this exercises `private_terms()` too -- and so the assertion does not
    depend on the internal runner's variable being set, which it is not on a
    developer's machine, where the real check skips.
    """
    monkeypatch.setenv("CLIFFRACER_PRIVATE_TERMS", "plutonium")
    planted = _write(
        tmp_path,
        "nightly.yml",
        SCHEDULE_ONLY.replace("  group: nightly", "  group: nightly  # runs on plutonium"),
    )

    clean = _write(tmp_path, "clean.yml", SCHEDULE_ONLY)

    assert private_term_hits([clean]) == [], (
        "the scan reports on a clean file, so the assertion below would pass for the wrong reason"
    )

    reported = private_term_hits([planted])

    assert reported, (
        "the private-term scan reported nothing for a schedule-only workflow whose "
        "comment names a term, so exempting such a file from the pipeline rules "
        "would also exempt it from this one"
    )


# --- controls ----------------------------------------------------------------


def _write(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    return path


CI_SHAPED = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: uv run pytest
"""

SCHEDULE_ONLY = """\
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'
  workflow_dispatch:
concurrency:
  group: nightly
jobs:
  soak:
    runs-on: bench-tier
    steps:
      - run: docker run -d nats:2.10.29-alpine
"""


def test_CONTROL_a_second_file_with_a_push_trigger_is_still_a_pipeline(tmp_path: Path):
    """The rule this exemption must not weaken.

    A second CI pipeline in a platform's directory is the thing
    `test_every_platform_holds_exactly_one_workflow` exists to catch, and this
    change must not give it a way out. Two push-triggered files count as two,
    so that test still reds.
    """
    _write(tmp_path, "ci.yml", CI_SHAPED)
    _write(tmp_path, "also-ci.yml", CI_SHAPED.replace("name: CI", "name: CI Two"))

    found = ci_workflow_files("gitea", directory=tmp_path)

    assert len(found) == 2, [p.name for p in found]


def test_CONTROL_a_schedule_only_file_does_not_count_as_a_pipeline(tmp_path: Path):
    """The other direction, so the control above is not the whole story."""
    _write(tmp_path, "ci.yml", CI_SHAPED)
    _write(tmp_path, "nightly.yml", SCHEDULE_ONLY)

    found = ci_workflow_files("gitea", directory=tmp_path)

    assert [p.name for p in found] == ["ci.yml"], [p.name for p in found]


def test_CONTROL_adding_a_push_trigger_reclassifies_it(tmp_path: Path):
    """Stated as a transition, because that is how this will be met in practice.

    Someone adds `push:` to a nightly workflow; every pipeline rule then applies
    to it, and one-per-platform is the first to fail. That is the intended
    behaviour of a derived distinction and the reason it is not a name list.
    """
    before = _write(tmp_path, "nightly.yml", SCHEDULE_ONLY)
    assert not is_ci_pipeline(load(before))

    after = _write(
        tmp_path,
        "nightly.yml",
        SCHEDULE_ONLY.replace("on:\n", "on:\n  push:\n    branches: [main]\n"),
    )

    assert is_ci_pipeline(load(after))
    _write(tmp_path, "ci.yml", CI_SHAPED)
    assert len(ci_workflow_files("gitea", directory=tmp_path)) == 2, (
        "a nightly workflow that gained a push trigger must count toward "
        "one-per-platform, or the exemption is a way out of the gate rules"
    )


def test_CONTROL_the_image_rule_catches_an_unpinned_one():
    """So the pinned-image assertion is not vacuous on a tree that has none."""
    assert unpinned(docker_images("docker run -d --name x nats -js\n")) == ["nats"]
    assert unpinned(docker_images("docker run redis:latest\n")) == ["redis:latest"]


def test_CONTROL_the_image_rule_accepts_a_pinned_one():
    """And that it is not "always report", which the control above would satisfy."""
    assert unpinned(docker_images("docker run -d --name x nats:2.10.29-alpine -js\n")) == []


def test_CONTROL_the_walker_finds_the_image_and_not_a_flag_value():
    """The bug this replaced: the first plausible token is usually --name's value.

    The real soak line, templating and port mappings included. A reader of a
    regex version would have seen `github.run_id` reported as an unpinned image.
    """
    line = (
        "docker run -d --name nats-chaos-${{ github.run_id }}-${{ github.run_attempt }} "
        "-p 4223:4223 -p 8223:8222 nats:2.10.29-alpine -js --port 4223 -m 8222\n"
    )

    assert docker_images(line) == ["nats:2.10.29-alpine"]
    assert unpinned(docker_images(line)) == []


def test_CONTROL_a_templated_image_is_not_judged():
    """It cannot be read as pinned or unpinned, and guessing either way is wrong."""
    images = docker_images("docker run -d ${{ env.IMAGE }} -js\n")

    assert images == ["TEMPLATE"], images
    assert unpinned(images) == []


def test_the_platform_directories_are_the_ones_the_guards_read():
    """A floor: this file's exemptions are scoped to the same directories.

    If a third platform directory were added and only the CI guards learned
    about it, the non-CI rules here would silently not apply there.
    """
    assert set(PLATFORM_DIRS) == {"gitea", "github"}, sorted(PLATFORM_DIRS)


def test_CONTROL_the_concurrency_rule_catches_a_workflow_without_a_group():
    """So the concurrency assertion means something on a tree with no soak yet."""
    import yaml

    without = yaml.safe_load(SCHEDULE_ONLY.replace("concurrency:\n  group: nightly\n", ""))
    assert concurrency_group(without) is None

    with_group = yaml.safe_load(SCHEDULE_ONLY)
    assert concurrency_group(with_group) == "nightly"

    bare = yaml.safe_load(
        SCHEDULE_ONLY.replace("concurrency:\n  group: nightly", "concurrency: nightly")
    )
    assert concurrency_group(bare) == "nightly", "a bare string group must be read too"


def test_CONTROL_the_trigger_rule_reads_all_three_on_spellings():
    """`on:` is a mapping here, but a list and a bare string are both legal."""
    import yaml

    assert trigger_names(yaml.safe_load("on:\n  push:\n    branches: [main]\n")) == {"push"}
    assert trigger_names(yaml.safe_load("on: [push, schedule]\n")) == {"push", "schedule"}
    assert trigger_names(yaml.safe_load("on: schedule\n")) == {"schedule"}
    assert trigger_names(yaml.safe_load("name: x\n")) == set()

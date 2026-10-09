"""Workflow prose describes the pipeline, not how it came to be.

The documentation guard owns the pattern set; this imports it, as the source
sweep and the commit message check do, so the four cannot drift. Every pattern
applies here: a workflow comment has none of the API-prose meanings that excuse
three of them from source, where "no longer present" describes a runtime state
rather than a change someone made.

Two kinds of prose are read. Comments, which is where a workflow explains
itself, and `name:` strings, which a run renders into its job and step list and
which are therefore read by anyone looking at a failure.

The private-term check reads a third kind, and only it does: the values a step
carries in `run:`, `env:` and `with:`. Those are not prose and the history
patterns must not reach them -- `run: git log 840b213` is a command, and a
sweep for commit SHAs that read commands would report every one of them. But a
name this repository does not publish does not arrive as prose. It arrives as
the URL that works, in a curl, and nobody writes it down as a note first.

Comments come from the YAML scanner rather than from a line match. A `#` inside
a quoted scalar is data -- `run: grep '#1234' file` is a command, not a note --
and a sweep that matched on the character alone would report it and force an
exemption for a line that never carried prose in the first place.
"""

import os
import re
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.repo.ci_workflows import PLATFORM_DIRS, rel, workflow_paths
from tests.repo.test_docs_carry_no_history import NUMBER_WAY_OUT, PATTERNS

pytestmark = pytest.mark.repo


REPO = Path(__file__).resolve().parents[2]

# Every documentation pattern applies to workflow prose. The three the source
# sweep excuses are excused there because a docstring describing an API says
# "used to guarantee" or "no longer present" about behaviour; a workflow
# comment saying either is describing its own past.
APPLIED = tuple(PATTERNS)

# What the shared set cannot know about, because it was written for prose about
# code rather than about the machines a job runs on. Each is a shape, not a
# name: a regex for a processor model carries nothing this repository would not
# publish, which a list of the hosts it runs on would.
WORKFLOW_PATTERNS = {
    "a run number": re.compile(r"\bruns?\s+\d{3,}\b", re.I),
    "a processor model": re.compile(
        r"\b(?:i[3579]-\d{4,5}[A-Za-z]*|Ryzen\s+\d+\s+\d{4}[A-Za-z]*|Xeon|EPYC)\b"
    ),
    "a core or thread count": re.compile(r"\b\d+\s*c\s*/\s*\d+\s*t\b", re.I),
    "a memory size": re.compile(r"\b\d+(?:\.\d+)?\s*(?:GiB|GB|MiB|MB)\b"),
}

# Names this repository does not publish cannot be written down in it: the tree
# mirrors to a public host, so a guard listing them would republish exactly what
# it exists to keep out. The internal runner supplies them instead, and a run
# without them says so rather than passing quietly.
# Keys whose VALUES the private-term check reads, on top of the prose the
# history sweep reads. A step's command, its environment and the inputs it
# passes are where a hostname actually turns up, because each is written to be
# what works rather than to be read.
SCANNED_VALUE_KEYS = ("run", "env", "with")

PRIVATE_TERMS_VAR = "CLIFFRACER_PRIVATE_TERMS"
PRIVATE_TERMS_NOT_RUN = (
    f"private-term check NOT RUN: {PRIVATE_TERMS_VAR} is unset, so no check was "
    "made for names this repository does not publish. The internal runner sets it."
)


def all_patterns() -> dict[str, re.Pattern[str]]:
    """The shared set plus the workflow-local shapes."""
    return {**{label: PATTERNS[label] for label in APPLIED}, **WORKFLOW_PATTERNS}


def private_terms() -> list[str]:
    """Terms supplied by the environment, or an empty list when unset."""
    raw = os.environ.get(PRIVATE_TERMS_VAR, "")
    return [term.strip() for term in raw.split(",") if term.strip()]


def comments(text: str) -> list[tuple[int, str]]:
    """Return (line, text) for every comment, reading the scanner's scalars.

    A `#` is a comment only outside a scalar token. Anything inside one is part
    of a value: a grep argument, a URL fragment, a colour.
    """
    spans = [
        (token.start_mark.index, token.end_mark.index)
        for token in yaml.scan(text)
        if isinstance(token, yaml.tokens.ScalarToken)
    ]
    found: list[tuple[int, str]] = []
    offset = 0
    for number, line in enumerate(text.splitlines(True), 1):
        position = line.find("#")
        while position != -1:
            index = offset + position
            if not any(start <= index < end for start, end in spans):
                found.append((number, line[position:].rstrip("\n")))
                break
            position = line.find("#", position + 1)
        offset += len(line)
    return found


def name_strings(node: Any) -> list[str]:
    """Every `name:` value in the document, at any depth."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "name" and isinstance(value, str):
                found.append(value)
            found.extend(name_strings(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(name_strings(item))
    return found


def _scalars_under(node: Any) -> list[Any]:
    """Every scalar node beneath one node, at any depth."""
    if isinstance(node, yaml.ScalarNode):
        return [node]
    if isinstance(node, yaml.SequenceNode):
        return [found for item in node.value for found in _scalars_under(item)]
    if isinstance(node, yaml.MappingNode):
        return [found for _key, value in node.value for found in _scalars_under(value)]
    return []


def value_scalars(text: str) -> list[tuple[int, str]]:
    """Return (line, text) for each line of every `run:`, `env:` or `with:` value.

    Composed rather than loaded, because `yaml.safe_load` discards position and
    a `run:` block is many lines long: a report naming the step but not the line
    sends an author looking through a script for a term the report will not
    repeat. Each line is located by its offset inside the node's own span, so
    the number is the file's line and not a count from the block's start.

    Mapping KEYS are not read. An `env:` key is a variable name the workflow
    chooses and a `with:` key is an action's input name, neither of which an
    author fills in with an address.
    """
    root = yaml.compose(text)
    if root is None:
        return []

    def walk(node: Any) -> list[Any]:
        found: list[Any] = []
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode) and key.value in SCANNED_VALUE_KEYS:
                    found.extend(_scalars_under(value))
                found.extend(walk(value))
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                found.extend(walk(item))
        return found

    lines: list[tuple[int, str]] = []
    for node in walk(root):
        start, end = node.start_mark.index, node.end_mark.index
        cursor = start
        for line in node.value.splitlines():
            if not line.strip():
                continue
            index = text.find(line, cursor, end)
            if index == -1:
                index = start
            else:
                cursor = index + len(line)
            lines.append((text.count("\n", 0, index) + 1, line))
    return lines


def history_in_workflows(paths: list[Path] | None = None) -> list[str]:
    """Return `path:line [label] text` for every pattern that matches."""
    targets = paths if paths is not None else [path for _, path in workflow_paths()]
    found: list[str] = []
    for path in targets:
        name = rel(path) if path.is_relative_to(REPO) else str(path)
        text = path.read_text()
        prose: list[tuple[int | str, str]] = list(comments(text))
        prose += [("name", value) for value in name_strings(yaml.safe_load(text))]
        patterns = all_patterns()
        for where, line in prose:
            for label, pattern in patterns.items():
                if pattern.search(line):
                    found.append(f"{name}:{where} [{label}] {line.strip()[:100]}")
    return found


def test_no_workflow_comment_or_name_narrates_history():
    found = history_in_workflows()
    assert not found, (
        "a workflow says what the pipeline does now. Move an issue number to "
        "the pull request, a commit reference to the changelog, and the reason "
        f"a job is shaped the way it is into a statement of the rule. {NUMBER_WAY_OUT}\n  "
        + "\n  ".join(found)
    )


def test_the_sweep_reads_both_platform_directories():
    """A sweep that opened no file, or only one platform's, would pass above."""
    paths = workflow_paths()
    assert paths, "no workflow files discovered"
    for platform in PLATFORM_DIRS:
        assert any(found == platform for found, _ in paths), f"{platform} yielded no workflow"
    assert all(path.exists() for _, path in paths)


def test_every_documentation_pattern_is_applied():
    """A pattern added to the documentation set reaches workflows too.

    Applying the set by name rather than by a copied list is what keeps this
    true, so this test guards the claim rather than a literal.
    """
    assert set(APPLIED) == set(PATTERNS)


def test_CONTROL_a_comment_naming_an_issue_is_caught(tmp_path: Path):
    path = tmp_path / "ci.yml"
    path.write_text("# see #1234 for why this step exists\nname: CI\njobs: {}\n")
    found = history_in_workflows([path])
    assert found, "a comment naming an issue was not caught"
    assert "[an issue or PR number]" in found[0], found


def test_CONTROL_a_comment_naming_a_commit_is_caught(tmp_path: Path):
    path = tmp_path / "ci.yml"
    path.write_text("# measured on commit 840b213\nname: CI\njobs: {}\n")
    found = history_in_workflows([path])
    assert found, "a comment naming a commit was not caught"
    assert "[a commit SHA]" in found[0], found


def test_CONTROL_a_step_name_naming_an_issue_is_caught(tmp_path: Path):
    """A `name:` is prose a run renders, so it is swept like a comment."""
    path = tmp_path / "ci.yml"
    path.write_text('name: CI\njobs:\n  t:\n    steps:\n      - name: "Workaround, see #77"\n')
    found = history_in_workflows([path])
    assert found, "a step name naming an issue was not caught"
    assert ":name [" in found[0], found


def test_CONTROL_a_hash_inside_a_quoted_scalar_is_not_a_comment(tmp_path: Path):
    """The near miss: a `#` that is an argument, not a note.

    The text after each `#` has to be something the patterns match, or this
    control passes whether or not the scanner is consulted: a line match would
    also return nothing, and the two readings would be indistinguishable. Here a
    line match yields `a commit SHA` and `a processor model`, and the span-aware
    reading yields nothing, so only the correct implementation is green.
    """
    path = tmp_path / "ci.yml"
    path.write_text(
        "name: CI\n"
        "jobs:\n"
        "  t:\n"
        "    steps:\n"
        "      - run: echo '#see commit 840b213'\n"
        '      - run: curl "http://example.test/x#i7-12700T"\n'
    )
    assert history_in_workflows([path]) == [], "a quoted scalar was read as a comment"


def test_CONTROL_a_trailing_comment_is_still_read(tmp_path: Path):
    """Excluding scalars must not cost the comments that follow one."""
    path = tmp_path / "ci.yml"
    path.write_text("name: CI  # see #4321 for the reason\njobs: {}\n")
    found = history_in_workflows([path])
    assert found, "a trailing comment was not read"
    # Line 1, not the name: the value there is "CI" and carries nothing.
    assert ":1 [" in found[0], found
    assert "[an issue or PR number]" in found[0], found


def test_CONTROL_ordinary_workflow_prose_is_not_caught(tmp_path: Path):
    """A comment stating the rule a job follows is what the sweep leaves alone."""
    path = tmp_path / "ci.yml"
    path.write_text(
        "# The benchmark runs in its own job on one hardware class, because two\n"
        "# measurements sharing a host move each other past the threshold that\n"
        "# scores them.\n"
        "name: CI\njobs: {}\n"
    )
    assert history_in_workflows([path]) == [], "ordinary workflow prose was read as history"


@pytest.mark.parametrize(
    ("line", "label"),
    [
        ("# steve is a Ryzen 7 3750H 4c/8t box", "a processor model"),
        ("# against an i7-12700T 20t machine", "a processor model"),
        ("# the box is 4c/8t", "a core or thread count"),
        ("# 125.47 GiB of memory", "a memory size"),
        ("# measured across runs 1868 and 1869", "a run number"),
        ("# reproduced on run 2002", "a run number"),
    ],
)
def test_CONTROL_each_workflow_pattern_catches_its_own_shape(tmp_path: Path, line: str, label: str):
    """The shapes the shared set cannot see, taken from what this guard exists for."""
    path = tmp_path / "ci.yml"
    path.write_text(f"{line}\nname: CI\njobs: {{}}\n")
    found = history_in_workflows([path])
    assert found, f"not caught: {line!r}"
    assert f"[{label}]" in found[0], found


@pytest.mark.parametrize(
    "line",
    [
        "# the step runs 12 times before giving up",
        "# run the suite on both platforms",
        "# keep the matrix to 2 entries",
        "# this job runs alone",
    ],
)
def test_CONTROL_a_near_miss_is_left_alone(tmp_path: Path, line: str):
    """A rule that catches its own shape proves nothing until it lets go of the next one."""
    path = tmp_path / "ci.yml"
    path.write_text(f"{line}\nname: CI\njobs: {{}}\n")
    assert history_in_workflows([path]) == [], f"wrongly caught: {line!r}"


def private_term_hits(paths: list[Path] | None = None) -> list[str]:
    """Return `path:where` for every supplied term in a workflow's prose or values.

    The location only. Naming the term, or quoting the line that carries it,
    would write the term into a failure message, a job log and whatever a
    reader pastes next -- which is the thing this check exists to prevent. The
    author knows which term they wrote; they need the line.
    """
    terms = private_terms()
    if not terms:
        return []
    targets = paths if paths is not None else [path for _, path in workflow_paths()]
    found: list[str] = []
    for path in targets:
        name = rel(path) if path.is_relative_to(REPO) else str(path)
        text = path.read_text()
        scanned: list[tuple[int | str, str]] = list(comments(text))
        scanned += [("name", value) for value in name_strings(yaml.safe_load(text))]
        scanned += value_scalars(text)
        for where, line in scanned:
            for term in terms:
                if re.search(rf"\b{re.escape(term)}\b", line, re.I):
                    found.append(f"{name}:{where} names a private term")
                    break
    return found


def test_no_workflow_names_a_private_term():
    """Names the environment says this repository does not publish.

    The tree mirrors to a public host, so the terms cannot live here. When they
    are not supplied this does not quietly pass: it skips with a reason, which
    `-ra` prints on every run, so a green suite still says the check was absent.
    """
    if not private_terms():
        warnings.warn(PRIVATE_TERMS_NOT_RUN, stacklevel=2)
        pytest.skip(PRIVATE_TERMS_NOT_RUN)
    found = private_term_hits()
    assert not found, (
        "a workflow names something this repository does not publish. State the "
        "rule instead of the machine. The term is not repeated here on purpose; "
        "the locations are:\n  " + "\n  ".join(found)
    )


def test_CONTROL_a_supplied_term_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """With the variable set, the check reds on a workflow naming the term."""
    monkeypatch.setenv(PRIVATE_TERMS_VAR, "zarquon, fnord")
    path = tmp_path / "ci.yml"
    path.write_text("# runs on zarquon only\nname: CI\njobs: {}\n")
    found = private_term_hits([path])
    assert found, "a supplied term was not caught"
    # The report must locate it without repeating it.
    assert ":1 names a private term" in found[0], found
    assert not any("zarquon" in line for line in found), (
        f"the report echoed the private term it is checking for: {found}"
    )


def test_CONTROL_a_term_in_a_step_name_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(PRIVATE_TERMS_VAR, "zarquon")
    path = tmp_path / "ci.yml"
    path.write_text('name: CI\njobs:\n  t:\n    steps:\n      - name: "Deploy to zarquon"\n')
    found = private_term_hits([path])
    assert found, "a supplied term in a step name was not caught"
    assert ":name names a private term" in found[0], found
    assert not any("zarquon" in line for line in found), found


def test_CONTROL_without_the_variable_nothing_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Unset means no check, which is why the skip above has to be visible."""
    monkeypatch.delenv(PRIVATE_TERMS_VAR, raising=False)
    path = tmp_path / "ci.yml"
    path.write_text("# runs on zarquon only\nname: CI\njobs: {}\n")
    assert private_term_hits([path]) == []


def test_CONTROL_a_term_in_a_run_body_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The case the check was blind to: a host in the command that uses it.

    Line 6, not line 1: a `run:` block is many lines and the report has to say
    which, because it will not repeat the term the author is looking for.
    """
    monkeypatch.setenv(PRIVATE_TERMS_VAR, "zarquon")
    path = tmp_path / "ci.yml"
    path.write_text(
        "name: CI\n"
        "jobs:\n"
        "  t:\n"
        "    steps:\n"
        "      - run: |\n"
        "          curl https://zarquon.example.test/api/v1/x\n"
        "          echo done\n"
    )
    found = private_term_hits([path])
    assert found, "a term in a run body was not caught"
    assert ":6 names a private term" in found[0], found
    assert not any("zarquon" in line for line in found), (
        f"the report echoed the private term it is checking for: {found}"
    )


def test_CONTROL_a_term_in_an_env_value_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(PRIVATE_TERMS_VAR, "zarquon")
    path = tmp_path / "ci.yml"
    path.write_text(
        "name: CI\njobs:\n  t:\n    steps:\n"
        '      - env:\n          API: "https://zarquon.example.test"\n        run: echo x\n'
    )
    found = private_term_hits([path])
    assert found, "a term in an env value was not caught"
    assert ":6 names a private term" in found[0], found


def test_CONTROL_a_term_in_a_with_input_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(PRIVATE_TERMS_VAR, "zarquon")
    path = tmp_path / "ci.yml"
    path.write_text(
        "name: CI\njobs:\n  t:\n    steps:\n"
        '      - uses: actions/checkout@v4\n        with:\n          repository: "zarquon/x"\n'
    )
    found = private_term_hits([path])
    assert found, "a term in a with input was not caught"
    assert ":7 names a private term" in found[0], found


@pytest.mark.parametrize(
    "command",
    [
        "curl https://zarquonium.example.test/x",
        "echo notzarquon",
        "python3 -c 'print(\"zarquon_prefixed_identifier\"[:3])'",
    ],
)
def test_CONTROL_a_near_miss_in_a_run_body_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
):
    """A rule that catches its own shape proves nothing until it lets go of the next one.

    Each of these contains the term as a substring and none of them names it.
    Without this, `in` would pass every test above while reporting a workflow
    that says `zarquonium`.
    """
    monkeypatch.setenv(PRIVATE_TERMS_VAR, "zarquon")
    path = tmp_path / "ci.yml"
    path.write_text(f"name: CI\njobs:\n  t:\n    steps:\n      - run: {command}\n")
    assert private_term_hits([path]) == [], f"wrongly caught: {command!r}"


def test_CONTROL_a_key_is_not_read_as_a_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keys are names the workflow or an action chose, and are documented as unread.

    Stated as a test rather than only in the docstring, because the difference
    is invisible: a walk that read keys too would pass every other control here.
    """
    monkeypatch.setenv(PRIVATE_TERMS_VAR, "zarquon")
    path = tmp_path / "ci.yml"
    path.write_text(
        "name: CI\njobs:\n  t:\n    steps:\n"
        '      - env:\n          zarquon: "safe"\n        run: echo x\n'
    )
    assert private_term_hits([path]) == []


def test_CONTROL_the_history_sweep_does_not_read_values(tmp_path: Path):
    """The extension is the private-term check's alone, and must stay that way.

    A `run:` legitimately names commits, issues and hardware -- `git log
    840b213`, `--cpus 4`. If the history patterns reached values, every such
    command would be reported and the sweep would be turned off by exemption.
    """
    # The whole value is double-quoted. Unquoted, YAML ends a plain scalar at
    # ` #`, so an unquoted `echo 'see #N'` would put a real comment on the line and the
    # sweep would be right to read it.
    path = tmp_path / "ci.yml"
    path.write_text(
        "name: CI\njobs:\n  t:\n    steps:\n      - run: \"git log 840b213 && echo 'see #1234'\"\n"
    )
    assert history_in_workflows([path]) == [], "the history sweep read a run body"


def test_the_value_sweep_reads_the_real_workflows():
    """Zero hits and zero lines read are the same report.

    The controls above run against documents written here; this asserts the
    extractor finds a plausible number of value lines in the workflows the
    check actually sweeps, so a compose that started returning nothing -- a
    renamed key, a changed loader -- is not read as a clean result.
    """
    counts = {rel(path): len(value_scalars(path.read_text())) for _, path in workflow_paths()}
    assert counts, "no workflow files discovered"
    total = sum(counts.values())
    assert total > 50, f"the value sweep read almost nothing: {counts}"
    assert sum(1 for n in counts.values() if n) >= 2, (
        f"only one workflow yielded any value lines: {counts}"
    )


def test_the_not_run_notice_is_printed_by_a_passing_run():
    """The notice has to reach the report, not just exist as a string.

    Driven through a real run with the variable unset, because a message that
    is only ever an argument to `skip` is not evidence that anyone sees it.
    """
    env = {k: v for k, v in os.environ.items() if k != PRIVATE_TERMS_VAR}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{Path(__file__).name}::test_no_workflow_names_a_private_term",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(Path(__file__).parent),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NOT RUN" in proc.stdout, proc.stdout[-2000:]
    assert PRIVATE_TERMS_VAR in proc.stdout, proc.stdout[-2000:]

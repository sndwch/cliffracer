"""scripts/release_note.py, tested before the one run that cannot be undone.

The release note is built once, on a push to main, after the tag is pushed and
the packages are published. There is no second chance and no dry run, so the
renderer is a file with tests rather than a heredoc in ci.yml.

Every case here builds a REAL repository with real commits and runs the real
renderer over it. A fixture of pre-formatted git output would test the fixture:
the thing most likely to be wrong is what `git log` actually emits for a
multi-paragraph body, which a hand-written fixture cannot be wrong about.
"""

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("release_note", ROOT / "scripts" / "release_note.py")
assert _spec is not None and _spec.loader is not None
release_note = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_note)


def _repo(tmp_path: Path, commits: list[str]) -> Path:
    d = tmp_path / "repo"
    d.mkdir()

    def run(*a):
        return subprocess.run(a, cwd=d, check=True, capture_output=True, text=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (d / "f").write_text("0")
    run("git", "add", "f")
    run("git", "commit", "-q", "-m", "chore: base")
    run("git", "tag", "v0.0.1")
    for i, message in enumerate(commits, 1):
        (d / "f").write_text(str(i))
        run("git", "add", "f")
        run("git", "commit", "-q", "-m", message)
    return d


def test_a_breaking_footer_is_not_read(tmp_path):
    """A `BREAKING CHANGE:` footer decides nothing here: a commit carrying one is listed by its
    subject and its footer is not copied into the note, which has no Breaking section without
    Breaking changelog entries."""
    d = _repo(
        tmp_path,
        [
            "feat!: the thing moved\n\n"
            "Some body prose.\n\n"
            "BREAKING CHANGE: X is gone. Use Y instead.\n\n"
            "Co-Authored-By: Someone <s@example.com>\n"
        ],
    )
    note = release_note.render("v0.0.1..HEAD", repo=str(d))

    assert "Breaking changes" not in note, note
    assert "X is gone" not in note, note
    assert note.startswith("## Commits") and "- feat!: the thing moved" in note, note


def test_with_no_breaking_entries_the_heading_is_ABSENT_not_empty(tmp_path):
    """A "## Breaking changes" heading over nothing reads as "we did not write
    it down", which is worse than not claiming to have any."""
    d = _repo(tmp_path, ["fix: something small", "docs: a note"])
    note = release_note.render("v0.0.1..HEAD", repo=str(d))

    assert "Breaking changes" not in note
    assert note.startswith("## Commits")
    assert "- fix: something small" in note


def test_WIP_subjects_are_dropped_from_the_list(tmp_path):
    d = _repo(tmp_path, ["WIP: half a thing", "feat: the whole thing"])
    note = release_note.render("v0.0.1..HEAD", repo=str(d))

    assert "- feat: the whole thing" in note
    assert "WIP:" not in note


def test_CONTROL_the_renderer_reads_the_range_it_is_given(tmp_path):
    """Without this, a renderer that returned "" for everything satisfies the
    absent-heading case, and one that read the whole history satisfies the
    rest."""
    d = _repo(tmp_path, ["fix: before the tag"])
    subprocess.run(["git", "tag", "v0.0.2"], cwd=d, check=True, capture_output=True)
    (d / "f").write_text("after")
    subprocess.run(["git", "add", "f"], cwd=d, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fix: after the tag"], cwd=d, check=True, capture_output=True
    )

    note = release_note.render("v0.0.2..HEAD", repo=str(d))
    assert "- fix: after the tag" in note
    assert "before the tag" not in note, "the renderer ignored the range it was given"


# --------------------------------------------------------------------------
# Renderer exit status verification
# --------------------------------------------------------------------------

_SWALLOWING = 'NOTES="$(%s)"\nif [ -z "$NOTES" ]; then echo EMPTY_RANGE; fi\n'
_CHECKED = (
    'if ! NOTES="$(%s)"; then echo RENDERER_FAILED; exit 1; fi\n'
    'if [ -z "$NOTES" ]; then echo EMPTY_RANGE; fi\n'
)


def _sh(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_a_renderer_failure_is_not_reported_as_an_empty_range():
    failing = "python3 -c 'import sys; sys.exit(3)'"

    swallowed = _sh(_SWALLOWING % failing)
    assert "EMPTY_RANGE" in swallowed.stdout, "control: the old shape must swallow it"
    assert swallowed.returncode == 0, "control: the old shape must succeed"

    checked = _sh(_CHECKED % failing)
    assert "RENDERER_FAILED" in checked.stdout
    assert "EMPTY_RANGE" not in checked.stdout, (
        "a renderer error must not also be reported as an empty range"
    )
    assert checked.returncode != 0


def test_a_genuinely_empty_range_still_reports_empty():
    """The checked shape must not turn every empty range into a failure --
    an assert that fires on intended behaviour is worse than none."""
    empty = "true"

    checked = _sh(_CHECKED % empty)
    assert "EMPTY_RANGE" in checked.stdout
    assert "RENDERER_FAILED" not in checked.stdout
    assert checked.returncode == 0


def test_the_renderer_actually_exits_nonzero_on_a_bad_range():
    """The status the shell above is checking has to exist.

    Without this, the shell fragment could be correct about a script that always
    exits 0, and the pair would still pass.
    """
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "release_note.py"), "no-such-ref..also-no-such"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode != 0, proc.stdout
    assert proc.stdout.strip() == "", "a failing render must not print a partial note"


def test_the_renderer_exits_with_code_2_on_usage_error():
    """Verify release_note.py exits with code 2 and usage instructions on bad arguments."""
    script = str(ROOT / "scripts" / "release_note.py")

    # Zero arguments provided
    proc_no_args = subprocess.run(
        [sys.executable, script],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc_no_args.returncode == 2
    assert proc_no_args.stdout.strip() == ""
    assert "usage: release_note.py <git range>" in proc_no_args.stderr

    # Multiple arguments provided
    proc_extra_args = subprocess.run(
        [sys.executable, script, "v0.0.1", "v0.0.2"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc_extra_args.returncode == 2
    assert proc_extra_args.stdout.strip() == ""
    assert "usage: release_note.py <git range>" in proc_extra_args.stderr


def test_merge_commits_are_excluded_from_commit_list(tmp_path: Path):
    """Verify merge commits are excluded from the release note commit listing."""
    d = tmp_path / "repo"
    d.mkdir()

    def run(*a):
        return subprocess.run(a, cwd=d, check=True, capture_output=True, text=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (d / "f").write_text("0")
    run("git", "add", "f")
    run("git", "commit", "-q", "-m", "chore: base")
    run("git", "tag", "v0.0.1")

    # Create feature branch and commit
    run("git", "checkout", "-q", "-b", "feature")
    (d / "feat_file").write_text("feat")
    run("git", "add", "feat_file")
    run("git", "commit", "-q", "-m", "feat: feature branch commit")

    # Return to main and merge with a merge commit
    run("git", "checkout", "-q", "main")
    (d / "main_file").write_text("main")
    run("git", "add", "main_file")
    run("git", "commit", "-q", "-m", "chore: main branch commit")
    run("git", "merge", "--no-ff", "-q", "-m", "Merge branch 'feature'", "feature")

    note = release_note.render("v0.0.1..HEAD", repo=str(d))
    assert "- feat: feature branch commit" in note
    assert "- chore: main branch commit" in note
    assert "Merge branch 'feature'" not in note


def non_stdlib_imports(source: str, filename: str = "<source>") -> set[str]:
    """Top-level module names imported by `source` that are not in the stdlib.

    One definition, called by the check and by its controls. A control that
    walked its own copy of the AST would go on passing after this walk drifted.
    """
    tree = ast.parse(source, filename=filename)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return {m for m in imported if m not in sys.stdlib_module_names and m != "__future__"}


def unavailable_imports(path: Path, seen: frozenset[Path] = frozenset()) -> set[str]:
    """What `path` imports that the runner's bare Python cannot provide.

    A sibling script in the same directory is importable, because the renderer
    puts its own directory on `sys.path`, but only as far as its own imports
    are: a sibling is followed, and whatever it imports is judged the same way.
    """
    missing: set[str] = set()
    for name in non_stdlib_imports(path.read_text(), str(path)):
        sibling = path.parent / f"{name}.py"
        if not sibling.is_file():
            missing.add(name)
        elif sibling not in seen:
            missing |= unavailable_imports(sibling, seen | {path})
    return missing


def test_the_renderer_runs_under_the_runners_python():
    """Verify release_note.py executes under runner Python without venv dependencies.

    The release note generation step in CI executes under the runner's system Python
    outside of the virtual environment. This test enforces that release_note.py
    relies exclusively on standard library modules and successfully renders under
    an isolated environment where PYTHONPATH and VIRTUAL_ENV are cleared.

    The range is ``HEAD``, which resolves at any clone depth. A two-ended range
    such as ``HEAD~1..HEAD`` makes ``git log`` exit 128 wherever the checkout is
    shallow, which reports a missing commit as a renderer failure.
    """
    import os
    import shutil

    # 1. AST check: ensure release_note.py imports only standard library modules
    script_path = ROOT / "scripts" / "release_note.py"
    unavailable = unavailable_imports(script_path)
    assert not unavailable, f"release_note.py imports what a bare Python lacks: {unavailable}"

    # 2. Execution check under runner python with clean environment
    clean_path = os.pathsep.join(
        x for x in os.environ.get("PATH", "").split(os.pathsep) if ".venv" not in x
    )
    runner_python = shutil.which("python3", path=clean_path) or "/usr/bin/python3"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    env["PYTHONPATH"] = ""
    env["VIRTUAL_ENV"] = ""
    if clean_path:
        env["PATH"] = clean_path

    rng = "HEAD"
    proc = subprocess.run(
        [runner_python, str(script_path), rng],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )
    assert proc.returncode == 0, (
        f"release_note.py exited {proc.returncode} rendering {rng!r} under "
        f"{runner_python} with PYTHONPATH and VIRTUAL_ENV cleared:\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "Commits" in proc.stdout, "Renderer did not produce rendered commit output"


def test_CONTROL_non_stdlib_import_in_release_note_fails_ast_check():
    """The check above must report a third-party import.

    It calls the same function the real check calls. Walking a copy of the AST
    here would keep passing after that walk drifted, which is the failure this
    control exists to rule out.
    """
    assert non_stdlib_imports("import sys\nimport subprocess\nimport nats\n") == {"nats"}


def test_CONTROL_a_from_import_of_a_third_party_module_is_reported():
    """The other spelling, since the walk handles them separately."""
    assert non_stdlib_imports("from nats.aio.client import Client\n") == {"nats"}


def test_CONTROL_a_sibling_script_is_judged_by_its_own_imports(tmp_path: Path):
    """A sibling that imports only the stdlib is available; one that imports a
    third-party module passes that module on as unavailable."""
    renderer = tmp_path / "renderer.py"
    renderer.write_text("import sys\nfrom helper import thing\n")
    (tmp_path / "helper.py").write_text("import json\nthing = 1\n")

    assert unavailable_imports(renderer) == set()

    (tmp_path / "helper.py").write_text("import json\nimport nats\nthing = 1\n")

    assert unavailable_imports(renderer) == {"nats"}


def test_CONTROL_an_import_with_no_sibling_script_is_unavailable(tmp_path: Path):
    renderer = tmp_path / "renderer.py"
    renderer.write_text("from helper import thing\n")

    assert unavailable_imports(renderer) == {"helper"}


def test_CONTROL_stdlib_and_future_imports_are_not_reported():
    """And it does not simply report everything."""
    assert non_stdlib_imports("from __future__ import annotations\nimport json\n") == set()


# A render call: release_note.py given the release's range. One definition: a control matching its
# own copy of this would keep passing after the real check's pattern drifted.
RENDER_CALL = 'python3 scripts/release_note.py "$RANGE"'


def unchecked_render_calls(text: str) -> list[str]:
    """Every line of `text` that renders the note without checking the renderer's exit status.

    A render call is checked when its line begins `if !`, whether it writes the note to a file
    (`if ! python3 ... > "$NOTE"; then`) or captures it (`if ! NOTES="$(python3 ...)"; then`).
    Anything else discards the status: `NOTES="$(cmd)"` keeps only the last status of the
    assignment, and `set -o pipefail` does not reach a command substitution.
    """
    return [
        line.strip()
        for line in text.splitlines()
        if RENDER_CALL in line and not line.lstrip().startswith("if ! ")
    ]


@pytest.mark.gitea_checkout
def test_ci_yml_actually_uses_the_checked_shape():
    """The Gitea release job renders the note with the renderer's exit status checked, writing it
    to a file, and renders it nowhere unchecked."""
    ci = (ROOT / ".gitea" / "workflows" / "ci.yml").read_text()

    assert (
        'if ! python3 scripts/release_note.py "$RANGE" --fragments-at "$TAG" > "$NOTE"; then' in ci
    ), "the release-note step no longer renders the note to a file with its exit status checked"
    assert not unchecked_render_calls(ci), unchecked_render_calls(ci)


def test_CONTROL_unchecked_ci_assignment_detected_at_any_indentation():
    """The check above must catch an unchecked render call wherever it sits.

    Calls the same matcher the real check calls, so the control cannot go on
    passing against a copy after that pattern drifts.
    """
    for indent in ("", "  ", "        ", "            "):
        for candidate in (
            f'{indent}NOTES="$(python3 scripts/release_note.py "$RANGE")"',
            f'{indent}python3 scripts/release_note.py "$RANGE" > "$NOTE"',
            f'{indent}python3 scripts/release_note.py "$RANGE" > "$NOTE" || true',
        ):
            assert unchecked_render_calls(candidate), (
                f"Unchecked call at indent {len(indent)} was not detected: {candidate}"
            )

    for checked in (
        '            if ! NOTES="$(python3 scripts/release_note.py "$RANGE")"; then',
        '            if ! python3 scripts/release_note.py "$RANGE" > "$NOTE"; then',
    ):
        assert not unchecked_render_calls(checked), (
            "the checked shape must not be reported as unchecked"
        )

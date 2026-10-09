"""Source comments and docstrings describe the code, not its past.

The documentation guard owns the pattern set; this imports it rather than
keeping a second copy, so the two cannot drift. Not every pattern transfers:
prose about an API legitimately says "used to guarantee" or "has been deleted",
meanings the documentation patterns read as history. Each pattern is therefore
either applied here or recorded as not applicable with its reason, and a test
fails if a new pattern is neither -- so adding one to the documentation set
forces a decision about source instead of silently skipping it.

Comments and docstrings are read through `tokenize` and `ast`, never as raw
lines. A `#` inside a string literal -- a NATS subject, a URL fragment, a colour
-- is not a comment, and a line-based sweep cannot tell the difference.
"""

import ast
import io
import os
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

from tests.repo.ci_workflows import (
    ci_workflow_paths,
    load,
    rel,
)
from tests.repo.test_docs_carry_no_history import NUMBER_WAY_OUT, PATTERNS

pytestmark = pytest.mark.repo


REPO = Path(__file__).resolve().parents[2]

COMMIT_CHECK = "scripts/check_commit_messages.py"

SOURCE_ROOTS = ("src", "tests", "scripts", "examples", "tools")

# Patterns from the documentation set that are applied to source.
APPLIED = (
    "an issue or PR number",
    "a commit SHA",
    "a 1.x comparison",
    "release narration",
    "rationale narration",
)

# The rest, with why prose about code is not prose about the project.
NOT_APPLIED: dict[str, str] = {
    "'used to' / 'no longer'": (
        "In an API description these are ordinary English with no historical "
        "sense: a hash is `used to` guarantee bounded size, and a key `no "
        "longer` present is a state the function returns a default for."
    ),
    "'before X existed'": (
        "The shape it catches is narration about the project's past, which in "
        "source reads as a description of ordering between two runtime events."
    ),
    "'was removed' / 'was replaced'": (
        "Passive voice about an entry, a key or a message being removed is how "
        "source describes what a function does to its own inputs."
    ),
}


def source_files() -> list[Path]:
    """Every tracked-shaped Python file in the trees this sweeps."""
    roots = [REPO / r for r in SOURCE_ROOTS]
    roots += sorted(p for p in (REPO / "packages").glob("*/src") if p.is_dir())
    roots += sorted(p for p in (REPO / "packages").glob("*/tests") if p.is_dir())
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(sorted(root.rglob("*.py")))
    return found


def prose_in(source: str) -> list[tuple[int, str]]:
    """Return (line, text) for every comment and docstring in `source`.

    Comments come from the tokeniser and docstrings from the syntax tree, so a
    `#` inside a string is never mistaken for a comment and a string that is
    not a docstring is never read as prose.
    """
    out: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                out.append((node.body[0].lineno, doc))
    return out


def history_in_source(paths: list[Path] | None = None) -> list[str]:
    """Return `path:line [label] text` for every applied pattern that matches."""
    found: list[str] = []
    for path in paths if paths is not None else source_files():
        rel = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)
        for lineno, text in prose_in(path.read_text()):
            for line in text.splitlines():
                for label in APPLIED:
                    if PATTERNS[label].search(line):
                        found.append(f"{rel}:{lineno} [{label}] {line.strip()[:100]}")
    return found


def test_every_documentation_pattern_is_applied_or_excused():
    """No pattern may be skipped by omission.

    Adding one to the documentation set and not deciding about source would
    otherwise narrow this sweep silently.
    """
    decided = set(APPLIED) | set(NOT_APPLIED)
    undecided = sorted(set(PATTERNS) - decided)
    assert not undecided, (
        f"these documentation patterns are neither applied to source nor "
        f"excused: {undecided}. Add each to APPLIED or to NOT_APPLIED with a reason."
    )
    stale = sorted(decided - set(PATTERNS))
    assert not stale, f"these name patterns the documentation set no longer has: {stale}"
    for label, reason in NOT_APPLIED.items():
        assert isinstance(reason, str) and reason.strip(), f"no reason recorded for {label}"


def test_the_sweep_reads_the_source_trees():
    """A sweep that matched no files would pass the check below."""
    files = source_files()
    assert len(files) > 150, f"only {len(files)} source files found; the sweep is not reading"
    for root in ("src/", "tests/", "packages/"):
        assert any(p.relative_to(REPO).as_posix().startswith(root) for p in files), (
            f"{root} not reached"
        )


def test_no_source_comment_or_docstring_narrates_history():
    found = history_in_source()
    assert not found, (
        "source comments and docstrings describe the code as it is. Move an "
        "issue number to the pull request, a commit reference to the changelog, "
        f"and a removal to CHANGELOG.md. {NUMBER_WAY_OUT}\n  " + "\n  ".join(found)
    )


def test_CONTROL_a_comment_naming_an_issue_is_caught(tmp_path: Path):
    """The shape this exists for: a reference back to the work that made a change."""
    path = tmp_path / "sample.py"
    path.write_text("# see #1234, this was refactored from foo\nX = 1\n")
    found = history_in_source([path])
    assert found, "a comment naming an issue was not caught"
    assert "[an issue or PR number]" in found[0], found


def test_CONTROL_a_narration_planted_in_a_package_test_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Package tests are swept like the repository's own tests.

    Built as a whole tree rather than handed to the sweep as a path, because
    what this pins is that `source_files()` reaches `packages/*/tests` at all.
    """
    planted = tmp_path / "packages" / "cliffracer-x" / "tests" / "test_x.py"
    planted.parent.mkdir(parents=True)
    planted.write_text('"""Unit tests for the x extension (#1234, #1235)."""\n')
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)

    assert planted in source_files(), source_files()
    found = history_in_source()
    assert found and "[an issue or PR number]" in found[0], found


def test_CONTROL_a_hash_inside_a_string_is_not_a_comment(tmp_path: Path):
    """A `#` in a string literal is data, and a line-based sweep cannot tell."""
    path = tmp_path / "sample.py"
    path.write_text('SUBJECT = "orders.created#42"\nURL = "http://x/y#1234"\n')
    assert history_in_source([path]) == [], "a string literal was read as a comment"


def test_CONTROL_a_docstring_naming_a_commit_is_caught(tmp_path: Path):
    """Docstrings are prose too, and are read through the syntax tree."""
    path = tmp_path / "sample.py"
    path.write_text('def f():\n    """Permitted per commit 7b6da13."""\n    return 1\n')
    found = history_in_source([path])
    assert found, "a docstring naming a commit was not caught"
    assert "[a commit SHA]" in found[0], found


def test_CONTROL_ordinary_api_prose_is_not_caught(tmp_path: Path):
    """The reason several documentation patterns are not applied here."""
    path = tmp_path / "sample.py"
    path.write_text(
        "def get(key, default=None):\n"
        '    """Return the value for `key`.\n\n'
        "    A SHA-256 hash is used to bound the header size. If the key has\n"
        "    been deleted, returns default.\n"
        '    """\n'
        "    return default\n"
    )
    assert history_in_source([path]) == [], "ordinary API prose was read as history"


def test_the_commit_message_check_applies_the_same_patterns():
    """The script and this sweep select the same labels from the shared set.

    Two selections of one pattern map is the shape that drifts: a pattern added
    to the source sweep and not the commit check would leave the log free of a
    rule the comments follow.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    from check_commit_messages import HISTORY_LABELS

    assert set(HISTORY_LABELS) == set(APPLIED), (
        "the commit-message check and this sweep select different patterns: "
        f"only the script {sorted(set(HISTORY_LABELS) - set(APPLIED))}, "
        f"only here {sorted(set(APPLIED) - set(HISTORY_LABELS))}"
    )


def test_every_workflow_runs_the_commit_message_check():
    """Both platforms run it, in a step with no condition of its own.

    A step-level `if:` would make the gate invisible to the CI-gate guard, and
    a check that runs on one platform only is green about the other.

    CI pipelines only. A workflow with neither a push nor a pull_request trigger
    has no pull request to check the messages of, and no test job to put the
    step in -- see tests/repo/test_a_scheduled_workflow_is_not_a_pipeline.py.
    """
    for _platform, path in ci_workflow_paths():
        data = load(path)
        steps = data.get("jobs", {}).get("test", {}).get("steps", [])
        running = [s for s in steps if COMMIT_CHECK in str(s.get("run", ""))]
        assert len(running) == 1, (
            f"{rel(path)} runs the commit-message check {len(running)} times; "
            "expected exactly one step"
        )
        assert "if" not in running[0], (
            f"{rel(path)} gates the commit-message step with a step-level `if:`; "
            "the script decides the event so the step stays unconditional"
        )


INSTALL_COMMAND = "uv sync --all-packages --extra dev"


def test_the_commit_message_check_runs_after_its_dependencies_are_installed():
    """It imports the pattern set from the test tree, so it needs the venv.

    The script reaches PATTERNS through `tests.repo.test_docs_carry_no_history`,
    which means pytest has to be importable when it runs. That holds because the
    step sits after the dependency install and runs under `uv run`. Moving it
    earlier, or spelling it `python3` the way the release-note step does, breaks
    it at run time and nowhere else -- so the ordering is asserted here.

    CI pipelines only, for the same reason as the check above: a workflow that
    gates nothing has no test job to order these steps within.
    """
    for _platform, path in ci_workflow_paths():
        steps = load(path).get("jobs", {}).get("test", {}).get("steps", [])
        runs = [str(s.get("run", "")) for s in steps]

        install_at = [i for i, r in enumerate(runs) if INSTALL_COMMAND in r]
        check_at = [i for i, r in enumerate(runs) if COMMIT_CHECK in r]
        assert len(install_at) == 1, f"{rel(path)}: expected one dependency install step"
        assert len(check_at) == 1, f"{rel(path)}: expected one commit-message step"

        assert install_at[0] < check_at[0], (
            f"{rel(path)} runs the commit-message check at step {check_at[0]}, "
            f"before dependencies are installed at step {install_at[0]}. It "
            "imports from the test tree and needs the environment."
        )
        assert runs[check_at[0]].strip().startswith("uv run "), (
            f"{rel(path)} runs the commit-message check as "
            f"{runs[check_at[0]].strip()!r}; it needs the project environment, "
            "so it runs under `uv run`."
        )


def test_the_commit_message_check_exists_where_the_workflows_call_it():
    """A workflow naming a script that is not there fails at run time, not here."""
    assert (REPO / "scripts" / "check_commit_messages.py").is_file(), (
        "the workflows run scripts/check_commit_messages.py and it is missing"
    )


def _ci_shaped_checkout(tmp_path: Path, subject: str) -> tuple[Path, str]:
    """Build an origin and a checkout shaped the way actions/checkout leaves one.

    The action fetches only the head commit, into refs/remotes/pull/N/head at
    depth 1. The base branch ref is never fetched, which is the state the check
    has to cope with.
    """

    def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
        )

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--quiet", "--initial-branch", "release/x")
    git(origin, "config", "user.email", "ci@example.com")
    git(origin, "config", "user.name", "CI")
    (origin / "a.txt").write_text("base\n")
    git(origin, "add", "a.txt")
    git(origin, "commit", "--quiet", "-m", "feat: the base commit")

    git(origin, "checkout", "--quiet", "-b", "feat/y")
    (origin / "b.txt").write_text("head\n")
    git(origin, "add", "b.txt")
    git(origin, "commit", "--quiet", "-m", subject)
    head = git(origin, "rev-parse", "HEAD").stdout.strip()

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "--quiet")
    git(checkout, "remote", "add", "origin", str(origin))
    git(
        checkout,
        "fetch",
        "--quiet",
        "--no-tags",
        "--depth=1",
        "origin",
        f"+{head}:refs/remotes/pull/1/head",
    )
    git(checkout, "checkout", "--quiet", "--force", "refs/remotes/pull/1/head")
    return checkout, head


def _run_commit_check(checkout: Path, base: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / COMMIT_CHECK.split("/")[-1])],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_BASE_REF": base,
            "COMMIT_CHECK_REPO": str(checkout),
        },
    )


def test_the_commit_message_check_sees_the_range_from_a_ci_shaped_checkout(tmp_path: Path):
    """It must resolve the base from the checkout CI actually produces.

    Running it against an ordinary clone proves nothing: every ref is present
    there. This builds the shallow, base-less checkout `actions/checkout`
    leaves and requires the check to reach the range from it.
    """
    checkout, _ = _ci_shaped_checkout(tmp_path, "feat: describe the code as it is")
    result = _run_commit_check(checkout, "release/x")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 commit message" in result.stdout, result.stdout


def test_the_commit_message_check_reds_on_a_ci_shaped_checkout(tmp_path: Path):
    """And having reached the range, it must still report what is in it."""
    checkout, _ = _ci_shaped_checkout(tmp_path, "fix: the thing from #1234, see commit 7b6da13")
    result = _run_commit_check(checkout, "release/x")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[an issue or PR number]" in result.stdout, result.stdout
    assert "[a commit SHA]" in result.stdout, result.stdout

"""A branch that deletes a test says so in the job log.

A change that removes a fix AND the test guarding it is green: the only thing
that would have objected left in the same commit. Every other guard here
protects against a check that cannot fail; this is the case where the check is
simply gone, and the suite reports the same "all passed" as a tree that still
has it.

NOT A GATE. Removing a test is often right -- a test whose subject is gone
should go with it -- so this prints and does not fail. What it buys is that the
removal arrives as a LINE rather than as an absence, and an absence has no
place in a diff where the eye naturally goes.

Collected from the MERGE BASE, never from the base branch tip. A two-dot
comparison against a moving `main` renders every test that landed after the
branch was cut as "removed", which is the artefact that produced the false
alarm this came from; a report that cries wolf is ignored inside a week.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "report_removed_tests.py"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _repo(tmp_path: Path, name: str = "r") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    return root


def _commit(root: Path, message: str, files: dict[str, str]) -> str:
    """Write *files* (real relative paths) and commit.

    Paths are spelled out rather than encoded in keyword names: an earlier
    version took `tests__test_x__py` and expanded `__` to `/`, which produced
    `tests/test_x/py` -- a directory called `test_x` holding a file called
    `py`. No `.py` file existed, so the reporter correctly found no tests and
    printed "removes 0 tests", and two of the tests below passed on that.
    """
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


def _report(root: Path, base: str) -> str:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--repo", str(root)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


TWO = """def test_kept():
    assert 1 == 1


def test_gone():
    assert 2 == 2
"""

ONE = """def test_kept():
    assert 1 == 1
"""

RENAMED = """def test_kept():
    assert 1 == 1


def test_gone_by_another_name():
    assert 2 == 2
"""

THREE = (
    TWO
    + """

def test_added():
    assert 3 == 3
"""
)


def test_a_removed_test_is_named(tmp_path):
    root = _repo(tmp_path)
    _commit(root, "base", {"tests/test_x.py": TWO})
    _git(root, "checkout", "-qb", "branch")
    _commit(root, "drop one", {"tests/test_x.py": ONE})

    out = _report(root, "main")

    assert "removes 1 test" in out, out
    assert "tests/test_x.py::test_gone" in out, out


def test_an_added_test_is_not_reported(tmp_path):
    """Additions are not the subject. Reporting them makes the output long
    enough that the removals stop being read, which is the only thing this has
    to protect."""
    root = _repo(tmp_path)
    _commit(root, "base", {"tests/test_x.py": TWO})
    _git(root, "checkout", "-qb", "branch")
    _commit(root, "add one", {"tests/test_x.py": THREE})

    out = _report(root, "main")

    assert "removes 0 tests" in out, out
    assert "test_added" not in out, out


def test_a_rename_with_an_unchanged_body_reads_as_a_rename(tmp_path):
    """The suite renames tests often. A rename counted as a removal is noise of
    exactly the kind that gets a report ignored."""
    root = _repo(tmp_path)
    _commit(root, "base", {"tests/test_x.py": TWO})
    _git(root, "checkout", "-qb", "branch")
    _commit(root, "rename", {"tests/test_x.py": RENAMED})

    out = _report(root, "main")

    assert "removes 0 tests" in out, out
    assert "renamed" in out, out
    assert "test_gone -> " in out and "test_gone_by_another_name" in out, out


def test_a_moved_base_branch_reports_nothing(tmp_path):
    """THE ARTEFACT THIS EXISTS TO AVOID. Tests that land on `main` after the
    branch is cut are not removals, and a two-dot comparison calls them one."""
    root = _repo(tmp_path)
    _commit(root, "base", {"tests/test_x.py": TWO})
    _git(root, "checkout", "-qb", "branch")
    _commit(root, "branch work", {"tests/test_y.py": "def test_branch():\n    assert 1\n"})
    _git(root, "checkout", "-q", "main")
    _commit(root, "main moves on", {"tests/test_z.py": "def test_later():\n    assert 1\n"})
    _git(root, "checkout", "-q", "branch")

    out = _report(root, "main")

    assert "removes 0 tests" in out, out
    assert "test_later" not in out, out


def test_the_zero_line_is_printed_on_a_clean_branch(tmp_path):
    """A report that says nothing when there is nothing is indistinguishable
    from a step that did not run."""
    root = _repo(tmp_path)
    _commit(root, "base", {"tests/test_x.py": TWO})
    _git(root, "checkout", "-qb", "branch")
    _commit(root, "touch nothing", {"readme.md": "hello\n"})

    out = _report(root, "main")

    assert "removes 0 tests" in out, out


def test_it_never_fails_the_job(tmp_path):
    """Not a gate: removing a test is often right."""
    root = _repo(tmp_path)
    _commit(root, "base", {"tests/test_x.py": TWO})
    _git(root, "checkout", "-qb", "branch")
    _commit(root, "drop one", {"tests/test_x.py": ONE})

    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "main", "--repo", str(root)],
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0, done.stdout + done.stderr


def test_the_workflow_runs_it_and_AGENTS_names_it():
    """A step nobody reads is not a report, and a step in no workflow is not a
    step."""
    workflow = (REPO / ".gitea" / "workflows" / "ci.yml").read_text()
    assert "report_removed_tests.py" in workflow, "no CI step runs the reporter"

    agents = (REPO / "AGENTS.md").read_text()
    assert "report_removed_tests" in agents, "AGENTS.md does not name the step"


def test_it_reports_on_the_repo_it_was_pointed_at_when_the_base_needs_fetching(tmp_path):
    """`--repo` has to reach the merge-base resolver, not only the AST walk.

    `check_commit_messages._git` runs `git -C git_root()`, and `git_root()`
    reads `COMMIT_CHECK_REPO` or the script's own repository -- so it never
    sees `--repo`, and changing the working directory cannot reach it because
    `-C` overrides the working directory.

    Invisible in CI, where `--repo` is `.` and the script's own repository IS
    the checkout, and invisible in an ordinary clone, where the base resolves
    locally and the resolver is never called. It bites in exactly the case the
    flag exists for: another checkout, whose base is not already present.

    This builds that case -- a shallow clone with no local base ref -- because
    no other test combines an unresolved base with `--repo`.
    """
    origin = _repo(tmp_path, "o")
    _commit(origin, "base", {"tests/test_a.py": TWO})

    work = tmp_path / "w"
    subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", f"file://{origin}", str(work)],
        check=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-qb", "branch")
    _commit(work, "drop one", {"tests/test_a.py": ONE})
    # The resolver has to FETCH the base, in the tree `--repo` names. Both refs
    # have to go: `git clone` leaves a local `main` as well as the remote one,
    # and with either present the script's fast path resolves the base and
    # never calls the resolver at all. The first version of this test deleted
    # only the remote ref, so it passed with the defect restored -- vacuous for
    # the one thing it exists to check.
    _git(work, "update-ref", "-d", "refs/remotes/origin/main")
    _git(work, "branch", "-D", "main")
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "main^{commit}"],
            cwd=work,
            capture_output=True,
            text=True,
        ).returncode
        != 0
    ), "the base still resolves locally, so this test is not exercising the fetch path"

    out = _report(work, "main")

    assert "removes 1 test" in out, out
    assert "tests/test_a.py::test_gone" in out, out

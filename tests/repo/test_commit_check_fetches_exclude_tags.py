"""Every fetch the commit-message check runs excludes tags.

The check deepens history to find a merge base. A fetch without ``--no-tags``
auto-follows any tag that points into the history it pulls down, and a tag in
the tree changes the version the build backend derives: a prerelease tag one
commit behind makes every packaging build fail. The check exists to read commit
messages, so it has no business changing what the tree's version resolves to.

The assertion reads the arguments the function would hand git, so it covers the
deepening fetch inside the loop as well as the explicit base fetch -- the loop
is the one that has no refspec and therefore the one that auto-follows.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_commit_messages.py"


def _load_script():
    """Import the checker as a module without running it."""
    spec = importlib.util.spec_from_file_location("_commit_check_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    """Stand-in for a completed git process."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def _drive_fetch_base(module, merge_base_returncode: int):
    """Run fetch_base against a recording git, returning the argv it used."""
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str, **kwargs: object) -> _Result:
        calls.append(args)
        if args[0] == "merge-base":
            return _Result(returncode=merge_base_returncode)
        if args[:2] == ("rev-parse", "--is-shallow-repository"):
            # The loop only deepens a shallow repository; answering "false" here
            # sends it down the unrelated-histories exit and the deepening fetch
            # never runs. The `len(fetches) > 1` assertion below is what catches
            # that, which is why it is there.
            return _Result(returncode=0, stdout="true\n")
        return _Result(returncode=0)

    module._git = fake_git  # type: ignore[attr-defined]
    module.fetch_base("main")
    return calls


def fetches_without_no_tags(calls) -> list[tuple[str, ...]]:
    """Every recorded fetch that does not exclude tags."""
    return [c for c in calls if c[0] == "fetch" and "--no-tags" not in c]


def test_no_fetch_in_the_base_lookup_can_follow_a_tag():
    """Both the base fetch and the deepening fetch exclude tags."""
    module = _load_script()
    # returncode 1 keeps the merge base unresolved, so the loop deepens and the
    # deepening fetch is actually exercised rather than skipped.
    calls = _drive_fetch_base(module, merge_base_returncode=1)

    fetches = [c for c in calls if c[0] == "fetch"]
    assert len(fetches) > 1, (
        f"the deepening fetch never ran, so this proves nothing about it: {fetches}"
    )
    assert fetches_without_no_tags(calls) == []


def test_CONTROL_a_fetch_without_the_flag_is_caught():
    """The reader rejects a fetch that omits the flag."""
    assert fetches_without_no_tags([("fetch", "--quiet", "--deepen=50", "origin")])


def test_CONTROL_a_fetch_with_the_flag_and_a_non_fetch_are_not_caught():
    """The reader accepts a fetch that carries the flag, and ignores other commands."""
    assert not fetches_without_no_tags(
        [
            ("fetch", "--no-tags", "--quiet", "--deepen=50", "origin"),
            ("merge-base", "origin/main", "HEAD"),
            ("rev-parse", "--verify", "origin/main^{commit}"),
        ]
    )


# The argv guard above reads what the function would run. The test below runs it
# for real, against an origin carrying a tag and a consumer shaped like the
# runner's checkout, and reads whether a tag arrived. It survives a restructure
# of the script that the argv guard would not, and it depends on no server state.


def _run(cmd: list[str], cwd: Path) -> str:
    """Run a git command in *cwd* and return its stdout."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout


def _tag_count(repo: Path) -> int:
    return len([line for line in _run(["git", "tag"], repo).splitlines() if line.strip()])


@pytest.fixture
def origin_and_consumer(tmp_path):
    """An origin with a tag two commits back, and a consumer shaped like a checkout.

    The shape matters in two ways, and getting either wrong makes the test green
    whatever the script does.

    The consumer is built with ``git init`` and ``git remote add``, the way the
    runner's checkout builds it -- deliberately NOT ``git clone --no-tags``,
    which writes ``remote.origin.tagopt=--no-tags`` into the config and
    suppresses tag auto-follow on every later fetch.

    The head is a commit on a branch off ``main~1``, not ``main`` itself, so the
    merge base is absent from a depth-1 consumer and the base lookup has to
    deepen to find it. With the head on ``main``'s tip the merge base resolves
    at once, the deepening fetch never runs, and nothing is exercised.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _run(["git", "init", "-q", "--initial-branch=main", "."], origin)
    _run(["git", "config", "user.email", "fixture@example.invalid"], origin)
    _run(["git", "config", "user.name", "fixture"], origin)
    for i in range(3):
        (origin / "f.txt").write_text(f"{i}\n")
        _run(["git", "add", "f.txt"], origin)
        _run(["git", "commit", "-q", "-m", f"commit {i}"], origin)
    first = _run(["git", "rev-parse", "HEAD~2"], origin).strip()
    _run(["git", "tag", "v1.0.0-dev.1", first], origin)

    _run(["git", "checkout", "-q", "-b", "pr", "HEAD~1"], origin)
    (origin / "p.txt").write_text("p\n")
    _run(["git", "add", "p.txt"], origin)
    _run(["git", "commit", "-q", "-m", "pull request commit"], origin)
    head_sha = _run(["git", "rev-parse", "HEAD"], origin).strip()
    _run(["git", "checkout", "-q", "main"], origin)

    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _run(["git", "init", "-q", "."], consumer)
    _run(["git", "remote", "add", "origin", str(origin)], consumer)
    _run(
        [
            "git",
            "fetch",
            "--no-tags",
            "--prune",
            "--depth=1",
            "origin",
            f"+{head_sha}:refs/remotes/pull/1/head",
        ],
        consumer,
    )
    _run(["git", "checkout", "-q", "--force", "refs/remotes/pull/1/head"], consumer)
    return consumer


def test_the_consumer_is_shaped_so_a_tag_can_reach_it(origin_and_consumer):
    """The fixture starts tagless, has no tagopt, and needs deepening to find its base."""
    consumer = origin_and_consumer

    assert _tag_count(consumer) == 0
    config = _run(["git", "config", "--local", "--list"], consumer)
    assert "tagopt" not in config, (
        "the consumer has tagopt set, which suppresses auto-follow and would make "
        f"the test below green whatever the script does: {config}"
    )
    _run(
        [
            "git",
            "fetch",
            "--no-tags",
            "--quiet",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ],
        consumer,
    )
    merge_base = subprocess.run(
        ["git", "merge-base", "origin/main", "HEAD"], cwd=consumer, capture_output=True
    )
    assert merge_base.returncode != 0, (
        "the merge base already resolves, so the deepening fetch will never run "
        "and the test below would prove nothing about it"
    )


def test_CONTROL_a_deepening_fetch_without_the_flag_pulls_the_tag_in(origin_and_consumer):
    """Without the flag the tag arrives, so a tagless result afterwards means something."""
    consumer = origin_and_consumer
    assert _tag_count(consumer) == 0

    _run(["git", "fetch", "--quiet", "--deepen=50", "origin"], consumer)

    assert _tag_count(consumer) == 1


def test_the_base_lookup_leaves_a_checkout_shaped_repo_tagless(origin_and_consumer, monkeypatch):
    """Running the real base lookup pulls no tag into the tree it deepens."""
    consumer = origin_and_consumer
    assert _tag_count(consumer) == 0

    module = _load_script()
    monkeypatch.setenv("COMMIT_CHECK_REPO", str(consumer))
    # Unpacked rather than compared against None: `fetch_base` returns a
    # (ref, reason, detail) triple, and a tuple is never None -- so
    # `assert module.fetch_base("main") is not None` would hold even when the
    # lookup failed and nothing was deepened.
    ref, reason, detail = module.fetch_base("main")
    assert ref is not None, f"the base lookup failed ({reason}: {detail}), so it deepened nothing"

    assert _tag_count(consumer) == 0

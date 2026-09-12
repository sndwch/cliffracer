"""scripts/release_note.py, tested before the one run that cannot be undone.

The release note is built once, on a push to main, after the tag is pushed and
the packages are published. There is no second chance and no dry run, so the
renderer is a file with tests rather than a heredoc in ci.yml.

Every case here builds a REAL repository with real commits and runs the real
renderer over it. A fixture of pre-formatted git output would test the fixture:
the thing most likely to be wrong is what `git log` actually emits for a
multi-paragraph body, which a hand-written fixture cannot be wrong about.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("release_note", ROOT / "scripts" / "release_note.py")
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


def test_a_breaking_footer_becomes_a_section_under_its_subject(tmp_path):
    d = _repo(
        tmp_path,
        [
            "feat!: the thing moved\n\n"
            "Some body prose that is not a footer.\n\n"
            "BREAKING CHANGE: X is gone. Use Y instead, which takes the same\n"
            "arguments and returns the same shape.\n\n"
            "Co-Authored-By: Someone <s@example.com>\n"
        ],
    )
    note = release_note.render("v0.0.1..HEAD", repo=str(d))

    assert note.startswith("## Breaking changes")
    assert "### feat!: the thing moved" in note
    assert "X is gone. Use Y instead, which takes the same" in note
    assert "arguments and returns the same shape." in note, "the footer's later lines were dropped"
    # The trailer ends the block; it is not part of the breaking change.
    assert "Co-Authored-By" not in note
    # And the prose above the footer is not swept in.
    assert "Some body prose" not in note


def test_with_no_footers_the_heading_is_ABSENT_not_empty(tmp_path):
    """A "## Breaking changes" heading over nothing reads as "we did not write
    it down", which is worse than not claiming to have any."""
    d = _repo(tmp_path, ["fix: something small", "docs: a note"])
    note = release_note.render("v0.0.1..HEAD", repo=str(d))

    assert "Breaking changes" not in note
    assert note.startswith("## Commits")
    assert "- fix: something small" in note


def test_the_phrase_in_prose_is_not_a_footer(tmp_path):
    """Line-anchored, because `body.count("BREAKING CHANGE:")` is not.

    Counting the 1.0 train's footers with a naive count attributed four to a
    `test:` commit that has none -- the phrase appeared in its prose.
    """
    d = _repo(
        tmp_path,
        ["fix: a fix\n\nThis is not a BREAKING CHANGE: it is backwards compatible.\n"],
    )
    note = release_note.render("v0.0.1..HEAD", repo=str(d))

    assert "Breaking changes" not in note, note


def test_WIP_subjects_are_dropped_from_the_list(tmp_path):
    d = _repo(tmp_path, ["WIP: half a thing", "feat: the whole thing"])
    note = release_note.render("v0.0.1..HEAD", repo=str(d))

    assert "- feat: the whole thing" in note
    assert "WIP:" not in note


def test_a_WIP_commit_still_contributes_its_breaking_footer(tmp_path):
    """Dropping the SUBJECT from the list must not drop the FOOTER: a breaking
    change is a breaking change whoever wrote it, and losing one silently is
    the failure this renderer exists to fix."""
    d = _repo(tmp_path, ["WIP: half a thing\n\nBREAKING CHANGE: Z is gone.\n"])
    note = release_note.render("v0.0.1..HEAD", repo=str(d))

    assert "Z is gone." in note
    assert note.count("- WIP:") == 0


def test_two_footers_in_one_commit_become_two_sections(tmp_path):
    d = _repo(
        tmp_path,
        [
            "feat!: two at once\n\n"
            "BREAKING CHANGE: the first one.\n\n"
            "BREAKING CHANGE: the second one.\n"
        ],
    )
    note = release_note.render("v0.0.1..HEAD", repo=str(d))

    assert "the first one." in note
    assert "the second one." in note
    assert note.count("### feat!: two at once") == 2


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


def test_the_renderer_runs_under_the_runners_python():
    """Verify release_note.py includes future annotations import for Python 3.10 compatibility."""
    import ast

    tree = ast.parse((ROOT / "scripts" / "release_note.py").read_text())
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    ), "release_note.py needs `from __future__ import annotations`"


def test_ci_yml_actually_uses_the_checked_shape():
    """Closes the limitation the block comment above admits.

    The shell fragment in these tests is REPRODUCED from ci.yml, not imported --
    YAML cannot be executed from a test. So the pair above could keep passing
    while the workflow drifted back to the swallowing form, which is the failure
    mode of every hand-copied fixture. This reads the workflow itself.
    """
    ci = (ROOT / ".gitea" / "workflows" / "ci.yml").read_text()

    assert 'if ! NOTES="$(python3 scripts/release_note.py "$RANGE")"; then' in ci, (
        "the release-note step no longer checks the renderer's exit status; "
        '`NOTES="$(cmd)"` discards it and set -o pipefail does not help a '
        "command substitution"
    )
    # And the bare form is gone, not merely accompanied.
    assert '\n          NOTES="$(python3 scripts/release_note.py' not in ci, (
        "an unchecked `NOTES=$(...)` assignment is still present"
    )


# --------------------------------------------------------------------------
# Parse boundaries for breaking change footers preceding commit message bodies.
# --------------------------------------------------------------------------

# The exact shape of the commit that exposed it: footer first, blank line,
# non-indented prose, then trailers.
_FOOTER_FIRST = """\
BREAKING CHANGE: max_restart_attempts is removed from ServiceConfig. It never
capped anything. Set auto_restart=False to stop restarting.

The release job writes release notes to Gitea releases and never commits, so
this file stops at v1.4.1 and stays there.

aiohttp and email-validator are declared in BOTH lists and a guard caught the
drift, which is what that guard is for.

Co-Authored-By: Someone <s@example.com>
"""

# The ordinary shape: body first, footer last.
_FOOTER_LAST = """\
The release job writes release notes to Gitea releases and never commits.

BREAKING CHANGE: max_restart_attempts is removed from ServiceConfig. It never
capped anything. Set auto_restart=False to stop restarting.

Co-Authored-By: Someone <s@example.com>
"""


def test_a_block_ends_at_a_blank_line_followed_by_prose():
    blocks = release_note.breaking_blocks(_FOOTER_FIRST)

    assert len(blocks) == 1, blocks
    block = blocks[0]
    assert block.startswith("max_restart_attempts is removed")
    assert "Set auto_restart=False to stop restarting." in block
    assert "release notes to Gitea" not in block, (
        "the commit's ordinary body was absorbed into the breaking change"
    )
    assert "email-validator" not in block
    assert "Co-Authored-By" not in block


def test_CONTROL_a_footer_last_message_is_unaffected():
    """The shape every other commit on the branch uses. If the fix changed this
    one too it would be trading one truncation for another."""
    blocks = release_note.breaking_blocks(_FOOTER_LAST)

    assert len(blocks) == 1, blocks
    assert blocks[0].startswith("max_restart_attempts is removed")
    assert "Set auto_restart=False to stop restarting." in blocks[0]
    assert "release notes to Gitea" not in blocks[0], "body ABOVE the footer leaked in"
    assert "Co-Authored-By" not in blocks[0]


def test_a_wrapped_footer_keeps_every_line_up_to_the_blank():
    """The continuation lines are the footer. Only the blank line ends it."""
    blocks = release_note.breaking_blocks("BREAKING CHANGE: one\ntwo\nthree\n\nunrelated prose\n")

    assert blocks == ["one\ntwo\nthree"]


def test_two_footers_separated_by_prose_are_both_kept():
    """Ending a block must not swallow a LATER footer."""
    blocks = release_note.breaking_blocks(
        "BREAKING CHANGE: first\n\nsome prose\n\nBREAKING CHANGE: second\n"
    )

    assert blocks == ["first", "second"], blocks


def test_a_blank_line_before_a_trailer_does_not_truncate_early():
    """A blank line followed by a TRAILER is the ordinary end of a message, and
    the block should end there for the trailer's reason rather than the prose
    rule -- same result, but it must not lose the line above the blank."""
    blocks = release_note.breaking_blocks(
        "BREAKING CHANGE: one\ntwo\n\nCo-Authored-By: Someone <s@example.com>\n"
    )

    assert blocks == ["one\ntwo"], blocks


# --------------------------------------------------------------------------
# Paragraph-initial matching for breaking change footer markers.
# --------------------------------------------------------------------------

# Marker wrapped onto its own line inside a paragraph.
_MARKER_MID_PARAGRAPH = """\
Nine of the ten footers on this branch looked right only because they are last
in their messages.

A block now ends at a blank line whose next non-empty line is neither another
BREAKING CHANGE: nor a trailer key.

Co-Authored-By: Someone <s@example.com>
"""

# Marker is the very first line of the body.
_MARKER_AT_BODY_START = """\
BREAKING CHANGE: max_restart_attempts is removed from ServiceConfig. It never
capped anything. Set auto_restart=False to stop restarting.

The release job writes release notes to Gitea releases and never commits.

Co-Authored-By: Someone <s@example.com>
"""

# Second marker opening a paragraph after a blank line.
_TWO_MARKERS_EACH_OPENING_A_PARAGRAPH = """\
BREAKING CHANGE: ServiceConfig rejects unknown fields. Passing a removed or
misspelled setting raises pydantic.ValidationError naming it.

BREAKING CHANGE: the ServiceConfig fields queue_group, max_restart_attempts and
five others are removed. Nothing read any of them.

Co-Authored-By: Someone <s@example.com>
"""


def test_a_marker_inside_a_paragraph_is_prose_not_a_footer():
    """A marker inside a paragraph is treated as prose, not a footer."""
    assert release_note.breaking_blocks(_MARKER_MID_PARAGRAPH) == []


def test_CONTROL_a_marker_at_the_start_of_the_body_is_a_footer():
    """A marker at the start of the body is recognized as a footer."""
    blocks = release_note.breaking_blocks(_MARKER_AT_BODY_START)

    assert len(blocks) == 1, blocks
    assert blocks[0].startswith("max_restart_attempts is removed")


def test_CONTROL_two_paragraph_opening_markers_give_two_blocks():
    """Multiple paragraph-opening markers each yield a footer block."""
    blocks = release_note.breaking_blocks(_TWO_MARKERS_EACH_OPENING_A_PARAGRAPH)

    assert len(blocks) == 2, blocks
    assert blocks[0].startswith("ServiceConfig rejects unknown fields")
    assert blocks[1].startswith("the ServiceConfig fields queue_group")


def test_CONTROL_the_fixtures_differ_only_in_where_the_marker_sits():
    """Verify footer block count distinction across fixtures."""
    counts = [
        len(release_note.breaking_blocks(_MARKER_MID_PARAGRAPH)),
        len(release_note.breaking_blocks(_MARKER_AT_BODY_START)),
        len(release_note.breaking_blocks(_TWO_MARKERS_EACH_OPENING_A_PARAGRAPH)),
    ]
    assert counts == [0, 1, 2], counts

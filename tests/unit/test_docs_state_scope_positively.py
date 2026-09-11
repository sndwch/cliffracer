"""Tests verifying documentation states framework scope positively."""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _require_git():
    if not (REPO / ".git").is_dir():
        pytest.skip("Not running inside a git repository (release tarball)")


EXEMPT = {
    "CHANGELOG.md",
    "docs/decisions.md",
}

# Framework identity subject patterns.
_SUBJECT = r"(?:cliffracer|core|the library|the framework|this package|the package)"

PATTERNS = {
    "an unmeasured-performance disclaimer": re.compile(
        r"\b(not been (formally )?benchmarked|not been measured|"
        r"no validated .{0,20}figures)\b",
        re.I,
    ),
    "a section listing what is absent": re.compile(
        r"^#+ .*\bwhat this does not do\b|^\*\*Not implemented\*\*", re.I | re.M
    ),
    "an inventory of absent integrations": re.compile(r"\bthere is (also )?no built-in\b", re.I),
    "an absence attributed to the framework": re.compile(
        rf"\b{_SUBJECT}\s+(?:has|have|carries|owns|provides|ships|exports|contains|keeps)\s+no\b"
        rf"|\b{_SUBJECT}\s+imports\s+none\b",
        re.I,
    ),
    "the framework described by what it never does": re.compile(rf"\b{_SUBJECT}\s+never\b", re.I),
}


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO / rel for rel in out if rel not in EXEMPT]


def paragraphs(text: str):
    """Yield (first line number, whitespace-joined text) for each paragraph."""
    para: list[str] = []
    start = 1
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        heading = stripped.startswith("#")
        if (heading or not stripped) and para:
            yield start, " ".join(para)
            para = []
        if heading:
            yield number, stripped
        elif stripped:
            if not para:
                start = number
            para.append(stripped)
    if para:
        yield start, " ".join(para)


def negative_scope_lines(paths=None) -> list[str]:
    found = []
    for doc in paths if paths is not None else tracked_markdown():
        rel = doc.relative_to(REPO) if doc.is_relative_to(REPO) else doc
        for number, text in paragraphs(doc.read_text()):
            for label, pattern in PATTERNS.items():
                if pattern.search(text):
                    found.append(f"{rel}:{number} [{label}] {text[:100]}")
    return found


@pytest.mark.unit
def test_the_sweep_reads_the_documentation():
    """ "0 lines" is what a clean tree and a broken file list look like alike."""
    docs = tracked_markdown()
    assert len(docs) >= 15, f"only found {len(docs)} tracked .md files"
    assert all(d.exists() for d in docs), "git listed a file that is not on disk"


@pytest.mark.unit
def test_the_exemptions_all_exist():
    missing = [rel for rel in EXEMPT if not (REPO / rel).exists()]
    assert not missing, f"exempt files that do not exist: {missing}"


@pytest.mark.unit
def test_no_document_enumerates_what_the_framework_does_not_do():
    found = negative_scope_lines()
    assert not found, (
        "documentation is for what the code does. Put a removed name in "
        "CHANGELOG.md, a still-binding scope decision in "
        "docs/decisions.md, and delete the rest rather than softening it:\n  " + "\n  ".join(found)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "End-to-end throughput has not been formally benchmarked.",
        "Throughput has not been measured.",
        "## What this does not do",
        "**Not implemented**:",
        "Note: there is no built-in Prometheus integration.",
        "Core has no notion of a caller identity.",
        "Each ships as its own distribution; core imports none of them.",
        "Persistence is yours: cliffracer ships no repository.",
        "Cliffracer exports no persistence-flavoured exception.",
        "The library never reads the environment.",
    ],
)
def test_CONTROL_each_pattern_catches_its_own_shape(tmp_path: Path, line: str):
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    assert negative_scope_lines([doc]), f"not caught: {line!r}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "`self.http.app` does not exist until the extension's setup() runs.",
        "The verbs are methods on the extension instance.",
        "`@cron` handlers need no extension declared.",
        # Valid positive contrast statements.
        "Use `@rpc`, not `@service.rpc`.",
        "A namespace is **disambiguation, not isolation**.",
        "Note `user_id`, not `id`, and `set[str]`, not `list`.",
        "The registry is a private index, not a PyPI mirror.",
        # The framework as subject, but of what it DOES.
        "Core serves `GET /health` and `GET /info` on its own listener.",
        "Cliffracer ships a `cliffracer run` command.",
    ],
)
def test_CONTROL_positive_prose_and_the_deliberate_keeps_pass(tmp_path: Path, line: str):
    """The other half. A checker that flags everything is not a checker.

    These are real sentences from the tree, so this asserts the guard's own
    scope and not merely that it can stay quiet. None of them is a survivor of
    the ruled-out shape: a control built from a line that is itself due for
    deletion would lock the guard out of ever patterning it.
    """
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    assert not negative_scope_lines([doc]), f"wrongly flagged: {line!r}"


# --- wrap controls -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "wrapped",
    [
        "End-to-end throughput has not been\nformally benchmarked.",
        "Note: there is no\nbuilt-in Prometheus integration.",
        "Core has\nno notion of a caller identity.",
        "Core\nimports none of them.",
        "The library\nnever reads the environment.",
    ],
)
def test_CONTROL_a_pattern_still_catches_its_shape_across_a_line_break(tmp_path: Path, wrapped):
    """Ensure patterns match across line breaks within paragraphs."""
    doc = tmp_path / "d.md"
    doc.write_text(wrapped + "\n")
    assert negative_scope_lines([doc]), f"not caught when wrapped: {wrapped!r}"


@pytest.mark.unit
def test_CONTROL_a_blank_line_still_ends_a_paragraph(tmp_path: Path):
    """Joining must not run two paragraphs together and invent a match.

    `has` ending one paragraph and `no` opening the next is not a sentence
    anybody wrote; if the join ignored blank lines it would be one the guard
    reported.
    """
    doc = tmp_path / "d.md"
    doc.write_text("Core has\n\nno notion of a caller identity.\n")
    assert not negative_scope_lines([doc])


@pytest.mark.unit
def test_the_reported_line_number_is_the_paragraphs_first(tmp_path: Path):
    """A guard that says "somewhere in this file" is one nobody acts on."""
    doc = tmp_path / "d.md"
    doc.write_text("intro\n\nfiller\n\nCore has\nno notion of identity.\n")
    found = negative_scope_lines([doc])
    assert found and found[0].split(":")[1].startswith("5"), found


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "Core and the library have no notion of a caller identity.",
        "The framework and the library have no scheduler.",
    ],
)
def test_CONTROL_a_compound_subject_is_still_the_framework(tmp_path: Path, line: str):
    """`have` must be included in the alternation to correctly match compound
    subjects (e.g. "The framework and the library have no notion of...").
    If removed, we lose coverage on ordinary plural prose.

    The measurement that justified the removal asked whether any element of
    `_SUBJECT` is plural. That is a different question from whether the
    alternative can fire, and it could not have returned True however reachable
    the branch was.
    """
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    assert negative_scope_lines([doc]), f"not caught: {line!r}"


@pytest.mark.unit
def test_CONTROL_a_heading_does_not_join_the_paragraph_under_it(tmp_path: Path):
    """The other boundary. A heading ends a block the way a blank line does.

    Neither line matches alone. Joined across the heading they read as "Core
    has no notion ...", a sentence nobody wrote -- the same invented match
    `test_CONTROL_a_blank_line_still_ends_a_paragraph` rules out for blank
    lines, and it arrives the same invisible way: when someone re-flows a
    section.
    """
    doc = tmp_path / "d.md"
    doc.write_text("## Core has\nno notion here is a heading break\n")
    assert not negative_scope_lines([doc])


@pytest.mark.unit
def test_CONTROL_a_heading_is_still_matched_on_its_own(tmp_path: Path):
    """Splitting on headings must not stop the guard reading them.

    Both patterns that can land on a heading: the one written for headings, and
    an absence claim that happens to be phrased as one.
    """
    for text in ("## What this does not do\n", "## Core has no scheduler\n"):
        doc = tmp_path / "d.md"
        doc.write_text(text)
        assert negative_scope_lines([doc]), f"not caught as a heading: {text!r}"

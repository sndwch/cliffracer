"""Every decision a tracked file cites is a record in `docs/decisions.md`.

A decision that is removed with the feature it governed leaves its number behind in the guards,
documents and comments that cited it. A reader who follows the citation finds nothing, and the rule
the citation stood for has no owner: one guard cited a decision that was removed with the three
distributions it was about, while the rule it enforced (core's dependencies) was still true. This reads
every `ADR-nnnn` in every tracked text file against the headings of the decisions file.

The numbers a guard builds as test data are not citations; the one file that does so is listed below with
its reason.
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
DECISIONS = REPO / "docs" / "decisions.md"
CITATION = re.compile(r"\bADR[- ]?(\d{4})\b")
SKIPPED_SUFFIXES = (".lock", ".png", ".svg", ".jpg", ".gif")

#: Files whose `ADR-nnnn` text is not a citation, with why.
EXEMPT_REASONS: dict[str, str] = {
    "tests/repo/test_adr_status_tracks_package_presence.py": (
        "Builds decision records of its own as test data, numbered high enough that no real "
        "decision is ever one of them."
    ),
}


def _headings(text: str) -> set[str]:
    return set(re.findall(r"^## ADR-(\d{4})", text, re.M))


def dangling(files: dict[str, str], headings: set[str]) -> dict[str, list[str]]:
    """The citations in `files` (path to text) that name no decision in `headings`."""
    found: dict[str, list[str]] = {}
    for path, text in files.items():
        if path in EXEMPT_REASONS:
            continue
        missing = sorted({n for n in CITATION.findall(text) if n not in headings})
        if missing:
            found[path] = [f"ADR-{n}" for n in missing]
    return found


def _tracked_text() -> dict[str, str]:
    names = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split("\n")
    files: dict[str, str] = {}
    for name in names:
        if not name or name.endswith(SKIPPED_SUFFIXES):
            continue
        try:
            files[name] = (REPO / name).read_text(errors="ignore")
        except OSError:
            continue
    return files


def test_every_cited_decision_is_in_the_decisions_file():
    headings = _headings(DECISIONS.read_text())
    assert len(headings) >= 20, (
        f"read {len(headings)} decisions; the heading pattern is reading nothing"
    )

    missing = dangling(_tracked_text(), headings)

    assert missing == {}, (
        "these files cite a decision that is not in docs/decisions.md. Cite the decision that holds "
        "the rule now, or record the rule in one:\n  "
        + "\n  ".join(f"{path}: {', '.join(numbers)}" for path, numbers in sorted(missing.items()))
    )


def test_CONTROL_a_citation_of_a_removed_decision_is_reported():
    absent = f"ADR-{9999}"

    assert dangling({"docs/x.md": f"see {absent} for why"}, {"0001"}) == {"docs/x.md": [absent]}


def test_CONTROL_a_citation_written_with_a_space_is_read_too():
    absent = f"ADR {9999}"

    assert dangling({"docs/x.md": f"see {absent}"}, {"0001"}) == {"docs/x.md": [f"ADR-{9999}"]}


def test_CONTROL_a_citation_of_a_present_decision_is_not():
    assert dangling({"docs/x.md": "see ADR-0001, and ADR 0001 again"}, {"0001"}) == {}


def without_a_reason(reasons: dict[str, str]) -> list[str]:
    """The exemptions whose reason is empty or only whitespace."""
    return sorted(path for path, reason in reasons.items() if not reason.strip())


def test_every_exemption_names_a_tracked_file_and_gives_a_reason():
    tracked = set(_tracked_text())

    assert set(EXEMPT_REASONS) <= tracked, sorted(set(EXEMPT_REASONS) - tracked)
    assert without_a_reason(EXEMPT_REASONS) == []


def test_CONTROL_an_exemption_with_no_reason_is_reported():
    assert without_a_reason({"a.py": "  ", "b.py": "a real reason", "c.py": ""}) == ["a.py", "c.py"]

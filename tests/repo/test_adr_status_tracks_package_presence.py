"""A decision that owns an extension says Accepted exactly when it exists.

The history and scope sweeps read `docs/decisions.md` for prose, and neither
judges a decision's status against what exists. That leaves two silent drifts,
in opposite directions: a package lands and its decision still says Proposed, so
a reader believes the invariants are not in force; or a decision is flipped to
Accepted with no package behind it, so a reader believes they are.

Ownership is DECLARED rather than inferred. A decision that governs an extension
carries `- **Package**: cliffracer-<name>` beside its status, and that line is
the only thing this reads. Scanning the prose for a distribution name cannot
work: a decision about what core serves can name an extension only to say where
other behaviour belongs, so a sweep over the body would judge it as being about
a package it merely cites -- and the advice it would print, flip this to
Accepted, would be wrong. ADR-0011 says a check parses structure rather than
matching text, and a guard written for that decision must not itself decide
from prose.

The convention is held from the side that matters rather than by policing
mentions. A package that some decision names AND that exists in `packages/`
must be owned by a Package line somewhere, so the bullet cannot be forgotten on
the extension that lands. A package nobody has written a decision about is not
this guard's business, which is most of `packages/`.
"""

import re
from pathlib import Path

import pytest

from tests.repo import test_docs_carry_no_history as history
from tests.repo import test_docs_state_scope_positively as scope

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
DECISIONS = REPO / "docs" / "decisions.md"

ACCEPTED = "Accepted"

# A recorded count rather than a floor. ADR-0019 owns cliffracer-actors. A
# decision that merely names a package decides something else and owns nothing,
# and a second owning decision is a change worth reading rather than one the
# check should absorb.
OWNING_DECISIONS = 1
KNOWN_STATUSES = frozenset({ACCEPTED, "Proposed"})

_HEADING = re.compile(r"^## (ADR-\d+):\s*(.+)$", re.M)
_STATUS = re.compile(r"^-\s+\*\*Status\*\*:\s*(\S+)\s*$", re.M)
_PACKAGE = re.compile(r"^-\s+\*\*Package\*\*:\s*(\S+)\s*$", re.M)
_MENTION = re.compile(r"\bcliffracer-[a-z0-9]+(?:-[a-z0-9]+)*\b")


class Decision:
    """One decision: its identifier, its status, and the extension it owns."""

    def __init__(self, identifier: str, title: str, body: str) -> None:
        self.identifier = identifier
        self.title = title
        status = _STATUS.search(body)
        self.status = status.group(1) if status else ""
        package = _PACKAGE.search(body)
        self.package = package.group(1) if package else None
        self.mentions = sorted(set(_MENTION.findall(body)))

    def __repr__(self) -> str:
        return f"{self.identifier} [{self.status}] owns={self.package}"


def decisions(text: str | None = None) -> list[Decision]:
    """Every decision in the document, in the order it appears."""
    content = text if text is not None else DECISIONS.read_text()
    parts = _HEADING.split(content)
    return [Decision(parts[i], parts[i + 1], parts[i + 2]) for i in range(1, len(parts) - 2, 3)]


def package_exists(name: str) -> bool:
    """A distribution, not merely a directory: it has to carry a pyproject."""
    return (REPO / "packages" / name / "pyproject.toml").is_file()


def mismatches(entries: list[Decision] | None = None) -> list[str]:
    """Every owning decision whose status disagrees with its package's presence."""
    found = []
    for decision in entries if entries is not None else decisions():
        if decision.package is None:
            continue
        present = package_exists(decision.package)
        accepted = decision.status == ACCEPTED
        if accepted and not present:
            found.append(
                f"{decision.identifier} is {ACCEPTED} but {decision.package} is not in packages/"
            )
        if not accepted and present:
            found.append(
                f"{decision.identifier} is {decision.status!r} but {decision.package} "
                "is in packages/"
            )
    return found


def unowned_existing_packages(entries: list[Decision] | None = None) -> list[str]:
    """Packages a decision names, that exist, and that no Package line owns."""
    parsed = entries if entries is not None else decisions()
    owned = {d.package for d in parsed if d.package}
    mentioned = {name for d in parsed for name in d.mentions}
    return sorted(name for name in mentioned - owned if package_exists(name))


def test_every_owning_decision_matches_whether_its_package_exists():
    assert not mismatches(), (
        "a decision and the workspace disagree. Either the extension landed and "
        "its decision still says Proposed, or the decision was accepted without "
        "one:\n  " + "\n  ".join(mismatches())
    )


def test_every_named_package_that_exists_is_owned_by_a_decision():
    """The bullet cannot be forgotten on an extension that has actually landed."""
    unowned = unowned_existing_packages()
    assert not unowned, (
        "these packages are named in a decision and exist in packages/, but no "
        "decision declares ownership with a Package line, so their status is "
        f"judged by nothing: {unowned}. Add the bullet to the decision that "
        "governs each."
    )


def test_every_status_is_one_this_check_understands():
    """An unknown word must fail here rather than read as 'not Accepted' above."""
    unknown = sorted({d.status for d in decisions() if d.status not in KNOWN_STATUSES})
    assert not unknown, (
        f"statuses this check has no rule for: {unknown}. Decide what each means "
        "for whether the package must exist, and add it to KNOWN_STATUSES."
    )


def test_the_sweep_read_the_decisions_and_found_the_ones_it_judges():
    """A positive reading: no mismatches and no decisions parsed look identical."""
    parsed = decisions()
    assert len(parsed) > 15, f"only parsed {len(parsed)} decisions; the reader is not reading"
    assert all(d.status for d in parsed), [d for d in parsed if not d.status]
    owning = [d for d in parsed if d.package]
    assert len(owning) == OWNING_DECISIONS, (
        f"{len(owning)} decisions declare a package, the recorded count is "
        f"{OWNING_DECISIONS}: {owning}"
    )
    assert any(d.mentions for d in parsed), "no decision names an extension at all"


def test_the_markdown_sweeps_read_the_document_for_prose_and_not_for_status():
    """The two sweeps open this file, so a drift in a Status line is none of theirs.

    Read from the sweeps' own file lists rather than from their source text, so
    an exemption added to either one is what fails this.
    """
    for module in (history, scope):
        read = {path.relative_to(REPO).as_posix() for path in module.tracked_markdown()}
        assert "docs/decisions.md" in read, (
            f"{module.__name__} no longer reads docs/decisions.md; the decision "
            "prose is then read by no sweep"
        )


def _one(identifier: str, status: str, body: str, package: str | None = None) -> Decision:
    bullets = f"- **Status**: {status}\n"
    if package:
        bullets += f"- **Package**: {package}\n"
    return Decision(identifier, "title", bullets + f"- **Decision**: {body}\n")


def test_CONTROL_accepted_with_no_package_is_caught():
    caught = mismatches([_one("ADR-0099", ACCEPTED, "ships it", package="cliffracer-nonesuch")])
    assert caught and "not in packages/" in caught[0], caught


def test_CONTROL_proposed_with_the_package_present_is_caught():
    caught = mismatches([_one("ADR-0098", "Proposed", "plans it", package="cliffracer-kv")])
    assert caught and "is in packages/" in caught[0], caught


def test_CONTROL_matching_states_are_left_alone():
    assert mismatches([_one("ADR-0097", ACCEPTED, "ships", package="cliffracer-kv")]) == []
    assert mismatches([_one("ADR-0096", "Proposed", "plans", package="cliffracer-nonesuch")]) == []


def test_CONTROL_a_decision_that_only_cites_an_extension_is_not_judged():
    """The shape that is why ownership is declared rather than inferred.

    A decision naming an extension in its rationale owns nothing. Judging it
    would report a mismatch and advise flipping it to Accepted, which would be
    wrong about a decision that is not about that package at all.
    """
    citing = _one("ADR-0095", "Proposed", "Note that `cliffracer-kv` already solves this.")
    assert citing.package is None
    assert mismatches([citing]) == []


def test_CONTROL_a_cited_package_that_exists_and_is_unowned_is_reported():
    """The other side: the bullet cannot be skipped on a package that has landed."""
    citing = _one("ADR-0094", "Proposed", "Builds on `cliffracer-kv`.")
    assert unowned_existing_packages([citing]) == ["cliffracer-kv"]


def test_CONTROL_a_cited_package_that_does_not_exist_is_not_reported():
    citing = _one("ADR-0093", "Proposed", "Will build on `cliffracer-nonesuch`.")
    assert unowned_existing_packages([citing]) == []


def test_CONTROL_an_owned_package_is_not_also_reported_as_unowned():
    owning = _one("ADR-0092", ACCEPTED, "ships it", package="cliffracer-kv")
    assert unowned_existing_packages([owning]) == []


def test_CONTROL_the_reader_parses_a_document_it_did_not_write():
    parsed = decisions(
        "# Decisions\n\n"
        "## ADR-0001: First\n"
        "- **Status**: Accepted\n"
        "- **Package**: cliffracer-kv\n"
        "- **Decision**: Isolates it.\n\n"
        "## ADR-0002: Second\n"
        "- **Status**: Proposed\n"
        "- **Decision**: Cites `cliffracer-kv` only.\n"
    )
    assert [d.identifier for d in parsed] == ["ADR-0001", "ADR-0002"]
    assert [d.package for d in parsed] == ["cliffracer-kv", None]
    assert [d.mentions for d in parsed] == [["cliffracer-kv"], ["cliffracer-kv"]]

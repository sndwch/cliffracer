"""Tests verifying documentation contains no historical or ticket narratives."""

import ast
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


# Only documents the sweep would otherwise report. An exemption on a document
# that matches nothing takes it out of the sweep for free, so
# test_every_exemption_is_load_bearing fails on one.
EXEMPT_REASONS: dict[str, str] = {
    "docs/benchmarks.md": (
        "Records the commit each baseline was measured at, which the commit-SHA "
        "pattern reads as narration."
    ),
}
EXEMPT = set(EXEMPT_REASONS)


# A document may be exempt from SOME labels rather than all of them. Narrating
# change is the changelog's job -- "no longer publishes the text" is the sentence
# that job invites, and five authors in one day wrote it and were rejected -- so
# CHANGELOG.md is released from the labels about change over time.
#
# It is NOT released from the rest, which is why this is keyed per label and not
# per file. An issue number belongs in the pull request that closes it and a
# commit SHA in the log; AGENTS.md says so, and a whole-file exemption would
# quietly admit both. The rationale label is deliberation, which no document
# here carries -- the phrasings it catches are in CHANGELOG_STILL_REFUSED,
# where they are data and not prose, because this file is swept too.
FRAGMENTS = "changelog.d/*.md"

NARRATION_LABELS = (
    "'used to' / 'no longer'",
    "'before X existed'",
    "'was removed' / 'was replaced'",
    "a 1.x comparison",
    "release narration",
)

PATTERN_EXEMPT_REASONS: dict[str, dict[str, str]] = {
    "CHANGELOG.md": dict.fromkeys(
        NARRATION_LABELS,
        "A changelog entry's subject IS the change. Saying what the code stopped "
        "doing is the phrasing that job invites, and it is the one every author "
        "reaches for first.",
    ),
    # One key for every fragment: a fragment is a CHANGELOG.md entry written
    # before release, and the assembly copies its text in unchanged.
    FRAGMENTS: dict.fromkeys(
        NARRATION_LABELS,
        "A fragment is a changelog entry awaiting release, and the assembly "
        "copies it into CHANGELOG.md verbatim, so it is held to exactly the "
        "changelog's rule.",
    ),
}


def exempt_labels(rel: Path | str) -> set[str]:
    """The labels this document is released from, by its repository-relative path.

    Keyed on the path rather than the file name, so a `CHANGELOG.md` vendored
    inside some subdirectory is held to the whole rule. A fragment is a file
    directly in `changelog.d/` other than its README, which explains the
    mechanism and is held to the whole rule like any document.
    """
    path = Path(rel)
    if path.parent == Path("changelog.d") and path.name != "README.md":
        return set(PATTERN_EXEMPT_REASONS[FRAGMENTS])
    return set(PATTERN_EXEMPT_REASONS.get(str(rel), {}))


PATTERNS = {
    # A reference lead-in is required. A bare number sign and digits is how an
    # ordinary numbered item is written -- "Delivery 3", "Worker 1" -- and
    # flagging those rejects a correct present-tense sentence.
    #
    # The lead-in is the word IMMEDIATELY before the number, with only
    # punctuation or a space between it and the digits. Adjacency rather than
    # nearness is what lets this list hold short common words -- "in", "on",
    # "for", "at", "of" -- which ordinary prose uses constantly near numbers.
    # A rule accepting them anywhere within a span fires on numbers they have
    # nothing to do with, including in the sentence explaining the rule.
    # Requiring the word to sit against the number admits the phrasings people
    # actually write -- "Measured on", "Added in", "Introduced by",
    # "Discussed in", "Workaround for" -- without paying that.
    #
    # A number that LEADS its clause has no lead-in to sit against -- an
    # issue number opening a sentence, a list of them in parentheses, one with
    # a possessive (the ISSUE_CITATIONS rows show each) -- so a second
    # alternative takes three or more digits on their own. Three is the floor
    # because the ordinary numbers this rule must pass are one or two digits
    # ("Worker #3", "#12 Elm Street"), and every issue number here has three
    # or more. It does not fire when the `#` is glued to a word, a path, an
    # entity or an opening quote: `orders.created#420`, `page#1234` and
    # `'#1234'` are data, and `#10b981` is a colour because \b needs the
    # digits to end.
    #
    # THE LIMIT, stated because it is a decision: a pattern cannot tell an
    # issue number from any other number written with a leading `#`, so an
    # order, invoice or port number written that way is caught too, and
    # LEADING_NUMBER_COSTS below pins that it is. The way out is to write such
    # a number without the `#` -- "order 10023", "port 8080" -- which every
    # guard's failure message says.
    #
    # A number cited without the `#` is caught too. After `PR`, `PRs` or `pull
    # request(s)`, nouns that name nothing else, two or more digits are a
    # citation, spaced or not. After `issue(s)` or `pull(s)`, which are also
    # verbs that take a count, the singular counts only where no word follows the
    # number (punctuation, the end of the line, a possessive), and the plural only
    # before a range or a list, since a third-person verb ends a sentence on its
    # count as often as a citation does. Neither fires
    # glued to a name or a path, on a single digit (a numbered plan's step), or
    # on a version. This comment cannot show those spellings, since the guard
    # reads it: ISSUE_CITATIONS and ORDINARY_NUMBERS hold them as data, and
    # HASHLESS_MISSES below pins what the rule leaves through.
    #
    # ISSUE_CITATIONS below lists every phrasing this must catch and
    # ORDINARY_NUMBERS every one it must leave alone; both are parametrised, so
    # narrowing this pattern reds the phrasing it drops, by name.
    "an issue or PR number": re.compile(
        r"(?:\b(?:issues?|PRs?|pull requests?|fix(?:es|ed)?|closes?|closed"
        r"|resolves?|resolved|reverts?|reverted|see|refs?|from"
        r"|in|on|by|per|after|before|following|for|at|about|of"
        r"|added|introduced|removed|discussed|tracked|workaround)"
        r"\b[^\w\n]{0,3}#\d+\b|\(#\d+\)"
        r"|(?<![\w&/#'\"])#\d{3,}\b"
        r"|(?<![\w./-])(?:PRs?\s?|pull\s+requests?\s)\d{2,}\b(?!\.\d)"
        r"|(?<![\w./-])(?:issue|pull)\s\d{2,}(?:'s\b|(?=[^\w\s'])|\s*$)(?!\.\d)"
        r"|(?<![\w./-])(?:issues|pulls)\s\d{2,}\s*(?:[-,&]|and\b)\s*#?\d)",
        re.I,
    ),
    # The lead-in is required. Bare hex of that length is also a digest, a
    # correlation id or a colour, and matching it on sight rejects prose that
    # is simply naming a value.
    #
    # A few characters are allowed between the lead-in and the hex, because a
    # document writes it with punctuation and emphasis around the label -- the
    # one live instance in this tree is a markdown bold-and-colon. Requiring
    # whitespace immediately after the keyword made the rule blind to it. The
    # short bound is what still keeps "committed to" and "a commitment of
    # 1234567 users" out, together with the word boundary after the keyword.
    "a commit SHA": re.compile(
        r"\b(?:merged as|merged in|SHA|commit)\b[^\n`]{0,4}\s*`?[0-9a-f]{7,40}`?\b",
        re.I,
    ),
    "'used to' / 'no longer'": re.compile(
        r"\b(used to|no longer|previously|formerly|until 1\.0|until #\d+|"
        r"before 1\.0|before the fix|since 1\.0)\b",
        re.I,
    ),
    # Match subordinate clauses with named entities (e.g., 'before X existed').
    "'before X existed'": re.compile(
        r"\bbefore\s+[`\w.()]+\s+(existed|was added|was introduced|landed|shipped)\b",
        re.I,
    ),
    # A comparison, not a mention. Naming a supported version states what the
    # code does now; a phrase that reaches back past a release does not. The
    # comparative word is what separates them, so it is required rather than
    # quoted here -- quoting the shape would trip this rule in its own file.
    "a 1.x comparison": re.compile(
        r"\b(?:from|since|than|versus|vs\.?|compared to|compared with)\s+1\.x\b",
        re.I,
    ),
    "'was removed' / 'was replaced'": re.compile(
        r"\b(was|were|has been|have been)\s+(removed|deleted|dropped|renamed|replaced)\b",
        re.I,
    ),
    # "the ruling" on its own is a noun phrase that can head an ordinary
    # sentence; it needs the verb that makes it a report of a past decision.
    "rationale narration": re.compile(
        r"\b(we decided|it was decided|the reason we"
        r"|the ruling (?:was|is that|said))\b",
        re.I,
    ),
    # Every alternative pairs a subject with a past-tense verb. A bare noun
    # phrase would match the NAME of a live mechanism: "the orphan sweep" is
    # what tests/repo/test_no_orphan_defs.py is, and prose describing it in the
    # present tense is not narration.
    "release narration": re.compile(
        r"\b(4\.0 (deleted|removed|replaced|split)|0\.0\.70 removed"
        r"|orphan sweep (removed|deleted|dropped|replaced|added)"
        r"|the 1\.0 split)\b",
        re.I,
    ),
}


def tracked_markdown() -> list[Path]:
    """Return tracked *.md files from git index, excluding exempt files."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO / rel for rel in out if rel not in EXEMPT]


def history_lines(paths=None) -> list[str]:
    found = []
    for doc in paths if paths is not None else tracked_markdown():
        rel = doc.relative_to(REPO) if doc.is_relative_to(REPO) else doc
        released = exempt_labels(rel)
        for number, line in enumerate(doc.read_text().splitlines(), 1):
            for label, pattern in PATTERNS.items():
                if label in released:
                    continue
                if pattern.search(line):
                    found.append(f"{rel}:{number} [{label}] {line.strip()[:100]}")
    return found


def test_the_sweep_reads_the_documentation():
    """Verify tracked markdown files exist and are discovered."""
    docs = tracked_markdown()
    assert len(docs) >= 15, f"only found {len(docs)} tracked .md files: {docs}"
    assert all(d.exists() for d in docs), "git listed a file that is not on disk"


def test_the_exemptions_all_exist():
    """Verify all exempt documentation files exist on disk."""
    missing = [rel for rel in EXEMPT if not (REPO / rel).exists()]
    assert not missing, f"exempt files that do not exist: {missing}"


def test_the_exemptions_have_documented_reasons():
    """Verify every exemption has a non-empty justification."""
    for rel, reason in EXEMPT_REASONS.items():
        assert isinstance(reason, str) and reason.strip(), f"Missing justification for {rel}"


def test_every_exemption_is_load_bearing():
    """Each exempt document must be one the sweep would otherwise report.

    An exemption on a document that matches nothing takes a file out of the
    sweep for free: it reads as a considered decision while protecting
    nothing, and it keeps protecting nothing after the reason stops applying.
    """
    dead = [rel for rel in sorted(EXEMPT) if not history_lines([REPO / rel])]
    assert dead == [], (
        "these documents are exempt from the history sweep but match none of "
        f"its patterns, so the exemption is doing nothing: {dead}"
    )


def test_no_document_narrates_its_own_history():
    found = history_lines()
    assert not found, (
        "documentation is for what the code does now. Move a removal to "
        "CHANGELOG.md, a still-binding decision to docs/decisions.md, and an "
        "old name a reader might search for to CHANGELOG.md -- CHANGELOG.md is "
        "released from the change-over-time labels, so the sentence that reads "
        f"naturally there is allowed there. {NUMBER_WAY_OUT}\n  " + "\n  ".join(found)
    )


# The way out of a false positive of the leading-number rule, in every guard's
# failure message so a reader who hits one is not left to rewrite correct prose
# into something else by guesswork.
NUMBER_WAY_OUT = (
    "A number written with a leading # and three or more digits is read as an "
    "issue reference whatever it means; write an example number without the #, "
    "as in order 10023."
)

# Every way of citing an issue that this rule catches. Each one is ordinary
# prose rather than a contrived string: these are the phrasings that turn up in
# real comments and commit messages, which is why the list runs long and why
# the short-word lead-ins -- "in", "on", "for", "at" -- have to be in it.
ISSUE_CITATIONS = [
    "See #368 for the measurement.",
    "Fixes #368.",
    "Closes #368.",
    "Resolves #368.",
    "Refs #368.",
    "From #368.",
    "Reverted #368.",
    "issue #368",
    "PR #368",
    "pull request #368",
    "(#368)",
    "Measured on #368 (runs 1868 and 1869).",
    "Added in #368.",
    "Introduced by #368.",
    "Per #368.",
    "After #368 landed.",
    "Following #368.",
    "Discussed in #368.",
    "Removed by #368.",
    "As of #368.",
    "Tracked at #368.",
    "Because of #368.",
    "Workaround for #368.",
    "See the note in #368 about this.",
    "Keeps only the first -- #368 restored, silently.",
    "Unit tests for the host extension (#368, #369, #370).",
    "#368's property: fifty concurrent calls describe once.",
    "This is one pattern, not the list `#368` proposes.",
    "The case from PR 2676's first head.",
    "See PR 2676 for the measurement.",
    "Reviewed on PR2676.",
    "pr 2676 added the row.",
    "Two PRs 2676 and 2677 touch it.",
    "Found in pull request 2676 and fixed in pull requests 2677.",
    "Measured in pull 2676.",
    "Filed as issue 2715.",
    "issue 2715's limit is stated.",
    "fix: correlation cluster issues 1234-1238",
    "Rows from issues 2715, 2716 and 2717.",
    "Covered by issues 2715 and 2716.",
    "Tracked in issue 2715, see there.",
]

# A number sign and digits that cites nothing. The rule exists to let these
# through, and the first is the comment above the pattern's own definition --
# which the proximity form flagged once the lead-in list was widened, because
# the `in` of "lead-in" sat within its bound.
ORDINARY_NUMBERS = [
    "A reference lead-in is required. A bare `#3` is an ordinary numbered item.",
    "Delivery #3",
    "Worker #1",
    "The matrix has 3 entries and #4 is spare.",
    "Tier #2 is the transport tier.",
    "The service listens on port 4222.",
    "Write an example number without the #, as in order 10023.",
    "A PR-ready build is version 2.1.",
    "The broker container is cliffracer-pr3-nats.",
    "The broker container is cliffracer-pr12-nats.",
    "The fixture lives in tests/fixtures/issue-42.json.",
    "The client issues 30 requests a second.",
    "Each fetch can pull 10 messages at a time.",
    "Issue 22.1 of the spec defines the header.",
    "Step 3 of the plan is PR 3.",
    "The client issues 30.",
    "Each worker pulls 10.",
]


# Citations without a `#` the rule lets through, each for a reason the comment
# above the pattern gives. Asserted so a reader who meets one in a review finds
# it named as a limit, and so widening the rule to catch one has to weigh the
# verb sentences in ORDINARY_NUMBERS that come with it.
HASHLESS_MISSES = [
    # a word after the number: "issue" is a verb with a count as often
    "issue 2715 was filed for this.",
    # one digit: a numbered plan's steps are written this way
    "Added in PR 3.",
    # one plural number: a verb ends a sentence on its count the same way
    "Fixed with issues 2715.",
]


@pytest.mark.parametrize("line", HASHLESS_MISSES)
def test_CONTROL_a_hashless_citation_the_rule_leaves_through_is_named(tmp_path: Path, line: str):
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    assert history_lines([doc]) == [], (
        f"{line!r} now fires. If that is deliberate, move it to ISSUE_CITATIONS and check that "
        f"ORDINARY_NUMBERS' verb sentences still pass."
    )


@pytest.mark.parametrize("line", ISSUE_CITATIONS)
def test_CONTROL_every_way_of_citing_an_issue_is_caught(tmp_path: Path, line: str):
    """Each phrasing a person actually writes, not only the ones a forge emits."""
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    found = history_lines([doc])
    assert found, f"not caught: {line!r}"
    assert "[an issue or PR number]" in found[0], found


@pytest.mark.parametrize("line", ORDINARY_NUMBERS)
def test_CONTROL_a_number_that_cites_nothing_is_left_alone(tmp_path: Path, line: str):
    """The other half: a rule that caught these would reject correct prose.

    Without this the lead-in list could be widened until it matched anything,
    and every row above would still pass.
    """
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    assert history_lines([doc]) == [], f"wrongly caught: {line!r}"


# The price of adjacency, paid deliberately and pinned here so it is visible.
#
# A lead-in sitting against the number is what admits the thirteen phrasings
# the proximity rule missed -- the "Measured on", "Added in" and "Tracked at"
# shapes in ISSUE_CITATIONS. Those need bare `on`, `in` and `at` to count as
# lead-ins, and there is no version of that which does not also match an
# ordinary sentence putting the same preposition before an ordinary number.
# The trade is structural, not a gap in the word list.
#
# So these sentences DO fire, and that is the rule working as designed rather
# than a defect. They are asserted rather than left undiscovered for two
# reasons: a reader meeting one of these reds should see it named here as a
# known cost instead of concluding the guard is broken and rewording correct
# prose, and a future narrowing that fixes them reds this list by name so the
# thirteen phrasings it costs are counted at the same time.
#
# THE ALTERNATIVE WAS CONSIDERED AND REJECTED. Dropping the bare prepositions
# narrows this to the noun-led shapes in ORDINARY_NUMBERS and loses all
# thirteen citation phrasings -- the wrong side of the trade, since citations
# in prose are the thing the rule exists to catch and an address written as
# "#12 Elm Street" is not.
#
# Measured against the real tree: zero documents, source comments or workflow
# files contain a sentence of this shape today, so nothing is exempted for it
# and the cost is currently theoretical.
PREPOSITION_LED_FALSE_POSITIVES = [
    "Priorities are ranked by #1.",
    "The office is at #12 Elm Street.",
    "A retry of #3 is the last.",
    "The label on #2 is the transport tier.",
    "Requests for #5 are rejected.",
    "Rows added after #10 are ignored.",
    "The handler in #4 owns the subject.",
    "Everything before #7 is discarded.",
    "The column about #9 is unused.",
    "A queue of #6 entries drains first.",
    "The entry following #8 is a duplicate.",
]


@pytest.mark.parametrize("line", PREPOSITION_LED_FALSE_POSITIVES)
def test_CONTROL_a_preposition_before_an_ordinary_number_is_a_known_false_positive(
    tmp_path: Path, line: str
):
    """Adjacency cannot tell these from a citation, and that is the trade.

    Read the comment above before narrowing the lead-in list to make one of
    these pass: each one costs citation phrasings that ISSUE_CITATIONS pins,
    and the two lists are meant to be read together.
    """
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    hits = [h for h in history_lines([doc]) if "[an issue or PR number]" in h]
    assert hits, (
        f"{line!r} no longer fires. If that is deliberate, check what it cost: "
        f"the phrasings in ISSUE_CITATIONS are what bare prepositional lead-ins buy."
    )


def test_the_two_control_lists_disagree_about_prepositions_only(tmp_path: Path):
    """The must-fire and must-not-fire lists must differ on the moving axis.

    ORDINARY_NUMBERS is entirely noun-led -- Delivery, Worker, Tier -- so on its
    own it cannot show where adjacency stops, because no row puts a preposition
    against the number. This asserts the two lists actually straddle that axis,
    so neither can be quietly rewritten into agreement.
    """
    pattern = PATTERNS["an issue or PR number"]
    quiet = [line for line in ORDINARY_NUMBERS if pattern.search(line)]
    assert not quiet, f"ORDINARY_NUMBERS rows are firing: {quiet}"
    loud = [line for line in PREPOSITION_LED_FALSE_POSITIVES if not pattern.search(line)]
    assert not loud, f"known false positives stopped firing: {loud}"


# The cost of the leading-number rule, paid deliberately: a number with a
# leading `#` and three or more digits is caught whatever it means. Asserted so
# that a reader meeting one of these reds finds it named as a known cost, and
# so that narrowing the rule to let them through has to change this list.
LEADING_NUMBER_COSTS = [
    "Order #10023 was placed.",
]


@pytest.mark.parametrize("line", LEADING_NUMBER_COSTS)
def test_CONTROL_a_leading_number_that_is_not_an_issue_is_still_caught(tmp_path: Path, line: str):
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    hits = [h for h in history_lines([doc]) if "[an issue or PR number]" in h]
    assert hits, f"{line!r} no longer fires; if that is deliberate, update the limit in PATTERNS"


def test_CONTROL_the_way_out_of_a_leading_number_passes(tmp_path: Path):
    doc = tmp_path / "d.md"
    doc.write_text("Order 10023 was placed.\n")
    assert history_lines([doc]) == []


def test_the_rule_reads_the_real_tree_and_finds_it_clean(tmp_path: Path):
    """A positive reading: the sweep opened the documents before reporting none.

    "No citations found" and "no documents read" are the same output, so the
    count of documents is asserted alongside the absence.
    """
    docs = tracked_markdown()
    assert len(docs) >= 15, f"only {len(docs)} tracked documents; the sweep is not reading"
    assert history_lines(docs) == [], "a tracked document cites an issue"


@pytest.mark.parametrize(
    "line",
    [
        f"This was fixed in {chr(35)}103.",
        f"See {chr(35)}12345 for the issue report.",
        "Merged as 7730342, so the behaviour is now correct.",
        "The commit is 7730342abcdef.",
        "> **Baseline Commit**: `0f0db59237`",
        "Commit: 7730342abcdef",
        "`HTTPMixin` no longer exists.",
        "The database package was removed.",
        "We decided to split the packages.",
        "0.0.70 removed the mixin hierarchy.",
        "Before `RejectMessage` existed, the message reached the handler.",
        "The decorators are unchanged from 1.x.",
    ],
)
def test_CONTROL_each_pattern_catches_its_own_shape(tmp_path: Path, line: str):
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    assert history_lines([doc]), f"not caught: {line!r}"


# A present-tense sentence per pattern, each a single word away from the shape
# that pattern catches. A positive control shows a pattern is not dead; only a
# near miss shows where it stops, and the boundary is where a false positive
# lives. Keyed by pattern so the coverage test below is exact rather than a
# count.
NEAR_MISSES: dict[str, list[str]] = {
    "an issue or PR number": [
        "Delivery #1 is retried after 1.5s.",
        "Worker #3 handles the overflow queue.",
        "The subject `orders.created#1234` is data.",
        "Run `grep '#1234' file` to count them.",
        "The accent colour is #10b981.",
        "Room #42 is on the second floor.",
        "The anchor is docs/page#1234 on the site.",
    ],
    "a commit SHA": [
        "The payload digest is 7b6da13 for an empty body.",
        "A correlation id looks like 4f3c2b1a9d.",
    ],
    "'used to' / 'no longer'": [
        "A SHA-256 hash is used to bound the header size.",
    ],
    "'before X existed'": [
        "Before the broker starts, the service waits for it.",
    ],
    "a 1.x comparison": [
        "Version 1.x of the wire protocol is supported.",
    ],
    "'was removed' / 'was replaced'": [
        "A message that was dropped is redelivered.",
    ],
    "rationale narration": [
        "The ruling extension decides which handler runs.",
    ],
    "release narration": [
        "exempt fixtures from the orphan sweep, not private names",
        "the orphan sweep reports any unreferenced definition",
    ],
}

# Patterns kept strict for documents even though the near miss above shows they
# reject ordinary prose about code. Documents describe the project, where these
# phrasings do reach backwards; source and commit messages are held to the
# narrower set, recorded in tests/repo/test_source_carries_no_history.py.
DOCUMENT_ONLY_STRICTNESS: dict[str, str] = {
    "'used to' / 'no longer'": (
        "In an API description these are ordinary English -- a hash is used to "
        "bound a size -- but in prose about the project they reach past it."
    ),
    "'was removed' / 'was replaced'": (
        "Passive voice about a key or a message being removed is how source "
        "describes what a function does; in a document it narrates a change."
    ),
    "'before X existed'": (
        "In source this reads as ordering between two runtime events; in a "
        "document it is a claim about the project's past."
    ),
}


@pytest.mark.parametrize(
    ("label", "line"),
    [(label, line) for label, lines in NEAR_MISSES.items() for line in lines],
)
def test_CONTROL_a_near_miss_of_each_pattern_passes(tmp_path: Path, label: str, line: str):
    """The sentence one word away from each pattern must not match it.

    Two have already bitten: a commit subject naming the orphan sweep, and a
    `# Delivery #1:` comment read as an issue reference.

    For the patterns documents are deliberately held to, the near miss is
    checked against the sweep that does not apply them -- source comments and
    commit messages -- because that is where the sentence is ordinary prose.
    Asserting it against the document sweep would be asserting the opposite of
    the decision recorded in DOCUMENT_ONLY_STRICTNESS.
    """
    if label in DOCUMENT_ONLY_STRICTNESS:
        from tests.repo.test_source_carries_no_history import history_in_source

        sample = tmp_path / "sample.py"
        sample.write_text(f"# {line}\n")
        hits = [h for h in history_in_source([sample]) if f"[{label}]" in h]
        assert not hits, f"{label} wrongly flagged {line!r} in source: {hits}"
        return

    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    hits = [h for h in history_lines([doc]) if f"[{label}]" in h]
    assert not hits, f"{label} wrongly flagged {line!r}: {hits}"


@pytest.mark.parametrize(
    "line",
    [
        "The orphan sweep removed the dead helpers.",
        "The ruling was that extensions do not join the MRO.",
        "Reverted in #4312.",
        "Merged in 7730342abc.",
    ],
)
def test_CONTROL_the_tightened_patterns_still_catch_narration(tmp_path: Path, line: str):
    """And the other half, so tightening did not simply disable them."""
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    assert history_lines([doc]), f"not caught: {line!r}"


def test_every_pattern_has_a_near_miss():
    """A pattern added without one is a pattern nobody has bounded."""
    missing = sorted(set(PATTERNS) - set(NEAR_MISSES))
    assert not missing, (
        f"these patterns have no near miss, so nothing shows where they stop: "
        f"{missing}. Add a present-tense sentence one word from the shape they catch."
    )
    stale = sorted(set(NEAR_MISSES) - set(PATTERNS))
    assert not stale, f"near misses for patterns that no longer exist: {stale}"


def test_the_document_only_strictness_is_recorded_where_source_excludes_it():
    """The two files must agree about which patterns source is spared.

    Recording it in one place and excluding it in the other is how they drift.
    """
    from tests.repo.test_source_carries_no_history import NOT_APPLIED

    assert set(DOCUMENT_ONLY_STRICTNESS) == set(NOT_APPLIED), (
        "the patterns documents are held to but source is not disagree: "
        f"only here {sorted(set(DOCUMENT_ONLY_STRICTNESS) - set(NOT_APPLIED))}, "
        f"only in the source sweep {sorted(set(NOT_APPLIED) - set(DOCUMENT_ONLY_STRICTNESS))}"
    )
    for label, reason in DOCUMENT_ONLY_STRICTNESS.items():
        assert label in PATTERNS, f"{label} is recorded but is not a pattern"
        assert reason.strip(), f"no reason recorded for {label}"


def test_the_sha_pattern_matches_the_reference_the_exempt_document_carries():
    """The exemption and the pattern must not be able to disagree.

    `docs/benchmarks.md` is exempt because it records the commit a baseline was
    measured at. If the pattern stops matching that line the exemption silently
    becomes dead, and the tier that notices lives on the other side of a merge
    -- so this branch would be green and the merged tree red. Read from the file
    rather than copied, so a regenerated document is checked as it stands.
    """
    doc = REPO / "docs" / "benchmarks.md"
    assert doc.is_file(), "docs/benchmarks.md is missing; this control is stale"

    lines = [ln for ln in doc.read_text().splitlines() if "Baseline Commit" in ln]
    assert lines, "docs/benchmarks.md no longer records a baseline commit"

    pattern = PATTERNS["a commit SHA"]
    unmatched = [ln for ln in lines if not pattern.search(ln)]
    assert not unmatched, (
        "the commit-SHA pattern does not match the reference the exempt document "
        f"carries, so its exemption is doing nothing: {unmatched}"
    )


def test_CONTROL_ordinary_present_tense_prose_passes(tmp_path):
    """Verify standard present-tense prose is not flagged."""
    prose = (
        "Declare the extension as a class attribute. The attribute name is how "
        "you reach it and how its decorators are spelled. Build mutable state "
        "in setup(), not __init__."
    )
    doc = tmp_path / "prose.md"
    doc.write_text(prose + "\n")
    assert not history_lines([doc]), "present-tense prose was flagged"


# --- the changelog may narrate change; nothing else may ----------------------
#
# Five authors in one day wrote a changelog entry in the natural English for it
# -- "no longer publishes", "raises where it previously succeeded" -- and were
# rejected by a rule that is right for every other document. Each cost a red run
# and a reword, and nobody disagreed with the rule once they saw it.
#
# THE CURRENT CHANGELOG MATCHES NOTHING, because everyone has already reworded.
# So this exemption cannot be justified the way a whole-file exemption is, by
# pointing at what it currently suppresses -- `test_every_exemption_is_load_bearing`
# would call it dead. It is justified instead by the sentences it ADMITS, listed
# below and asserted in both directions: allowed in CHANGELOG.md, still rejected
# anywhere else.

CHANGELOG_MUST_ALLOW = [
    "- `ServiceConfig.log_level` was removed; set the level for the process instead.",
    "- The health endpoint no longer publishes an exception's own text.",
    "- A caller would previously have gone looking at its own payload.",
    "- Before `connect_timeout` existed, an unreachable broker could hang a dial.",
    "- Throughput is unchanged compared to 1.x.",
    "- The 1.0 split moved the extensions into their own packages.",
]

# Sentences the changelog is STILL held to. A pointer belongs in the pull request
# that closes it or in the log, and deliberation belongs in neither.
CHANGELOG_STILL_REFUSED = [
    ("- Fixes #1423 in the client emitter.", "an issue or PR number"),
    ("- Introduced in commit a1b2c3d4e5f.", "a commit SHA"),
    ("- We decided to gate this behind the config flag.", "rationale narration"),
]


def _sweep_one(tmp_path, monkeypatch, name: str, text: str) -> list[str]:
    """Run the real sweep over one document, at one repository-relative name.

    The name is what the exemption is keyed on, so it has to be the variable:
    a fake tree with the file at `name` inside it is the only way to ask "would
    this line be reported IN CHANGELOG.md" and "... in README.md" of the same
    sweep.
    """
    import tests.repo.test_docs_carry_no_history as guard

    doc = tmp_path / name
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text + "\n")
    monkeypatch.setattr(guard, "REPO", tmp_path)
    return guard.history_lines([doc])


@pytest.mark.parametrize("sentence", CHANGELOG_MUST_ALLOW)
def test_the_changelog_may_say_what_changed(sentence, tmp_path, monkeypatch):
    """The phrasings the rule was costing us, allowed where they belong."""
    assert _sweep_one(tmp_path, monkeypatch, "CHANGELOG.md", sentence) == []


@pytest.mark.parametrize("sentence", CHANGELOG_MUST_ALLOW)
def test_a_changelog_fragment_may_say_what_changed(sentence, tmp_path, monkeypatch):
    """A fragment is an entry awaiting release: the changelog's rule, not a looser one."""
    assert _sweep_one(tmp_path, monkeypatch, "changelog.d/a-topic.md", sentence) == []


@pytest.mark.parametrize(("sentence", "label"), CHANGELOG_STILL_REFUSED)
def test_a_changelog_fragment_is_still_held_to_the_rest(sentence, label, tmp_path, monkeypatch):
    found = _sweep_one(tmp_path, monkeypatch, "changelog.d/a-topic.md", sentence)

    assert any(f"[{label}]" in f for f in found), found


@pytest.mark.parametrize(
    "name",
    ["changelog.d/README.md", "changelog.d/nested/a-topic.md", "docs/changelog.d/a-topic.md"],
)
def test_CONTROL_only_a_fragment_directly_in_changelog_d_is_released(name, tmp_path, monkeypatch):
    """The README explains the mechanism and is documentation, and a directory of
    the same name elsewhere is not the one the assembly reads."""
    sentence = CHANGELOG_MUST_ALLOW[0]

    assert _sweep_one(tmp_path, monkeypatch, name, sentence), name


@pytest.mark.parametrize("sentence", CHANGELOG_MUST_ALLOW)
def test_CONTROL_the_same_sentence_still_reds_in_another_document(sentence, tmp_path, monkeypatch):
    """The exemption is the changelog's, not the rule's.

    Every sentence the test above allows must still be reported in an ordinary
    document -- otherwise this has quietly deleted the patterns rather than
    scoping them, and the four guards that share this pattern set would go on
    passing while protecting nothing.
    """
    found = _sweep_one(tmp_path, monkeypatch, "docs/some-guide.md", sentence)

    assert found, f"{sentence!r} is not reported outside CHANGELOG.md"


@pytest.mark.parametrize(("sentence", "label"), CHANGELOG_STILL_REFUSED)
def test_the_changelog_is_still_held_to_the_rest(sentence, label, tmp_path, monkeypatch):
    """Scoped per label, not per file.

    A whole-file exemption would admit an issue number and a commit SHA into the
    changelog, which AGENTS.md sends to the pull request and the log. This is
    what stops the exemption widening by accident.
    """
    found = _sweep_one(tmp_path, monkeypatch, "CHANGELOG.md", sentence)

    assert found, f"{sentence!r} should still be reported in CHANGELOG.md"
    assert any(f"[{label}]" in f for f in found), found


def test_every_pattern_exemption_names_a_real_label():
    """A typo in a label exempts nothing and reads as though it exempts something.

    The labels are long strings with quotes and slashes in them, which is
    exactly the shape a copy edit gets wrong.
    """
    for rel, labels in PATTERN_EXEMPT_REASONS.items():
        unknown = sorted(set(labels) - set(PATTERNS))
        assert not unknown, f"{rel} is exempt from labels that do not exist: {unknown}"


def test_every_pattern_exemption_is_load_bearing_for_a_sentence_we_need():
    """Each exempted label must be the one rejecting a sentence on the list above.

    This is the per-label answer to `test_every_exemption_is_load_bearing`: a
    whole-file exemption earns its place by what it currently suppresses, and
    this one by what it admits. An exempted label that no required sentence
    trips is doing nothing and should go.
    """
    needed = {
        label
        for sentence in CHANGELOG_MUST_ALLOW
        for label, pattern in PATTERNS.items()
        if pattern.search(sentence)
    }
    exempted = set(PATTERN_EXEMPT_REASONS["CHANGELOG.md"])

    idle = sorted(exempted - needed)
    assert not idle, (
        "CHANGELOG.md is exempt from labels that no sentence in "
        f"CHANGELOG_MUST_ALLOW trips, so the exemption is doing nothing: {idle}"
    )


def test_every_pattern_exemption_has_a_documented_reason():
    for rel, labels in PATTERN_EXEMPT_REASONS.items():
        for label, reason in labels.items():
            assert isinstance(reason, str) and reason.strip(), f"{rel}/{label} has no reason"


def test_CONTROL_the_exemption_is_keyed_on_the_path_not_the_name():
    """A vendored changelog somewhere else is held to the whole rule."""
    assert exempt_labels("CHANGELOG.md") == set(NARRATION_LABELS)
    assert exempt_labels("docs/CHANGELOG.md") == set()
    assert exempt_labels("packages/cliffracer-kv/CHANGELOG.md") == set()
    assert exempt_labels("README.md") == set()


def test_CONTROL_the_other_three_guards_do_not_see_this_exemption():
    """The pattern set is shared with the source, workflow and commit-message
    guards, which import PATTERNS and NUMBER_WAY_OUT and nothing else. A commit message or a
    docstring narrating history must still red, and this asserts the coupling
    rather than trusting that those modules never grow an import.
    """
    for module_path in (
        "tests/repo/test_source_carries_no_history.py",
        "tests/repo/test_workflows_carry_no_history.py",
        "scripts/check_commit_messages.py",
    ):
        text = (REPO / module_path).read_text()
        assert "PATTERN_EXEMPT_REASONS" not in text, module_path
        assert "exempt_labels" not in text, module_path
        imported = {
            alias.name
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.ImportFrom)
            and node.module == "tests.repo.test_docs_carry_no_history"
            for alias in node.names
        }
        assert "PATTERNS" in imported, f"{module_path} no longer shares the pattern set"
        assert imported <= {"PATTERNS", "NUMBER_WAY_OUT"}, (module_path, sorted(imported))

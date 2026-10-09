"""Source prose credits a finding to what it was, not to who found it.

Comments and docstrings in this tree named the identities that sessions on one
machine claim so that review is possible at all. Those names are true and they
are useless to a reader: an identity is claimed per session and released at the
end of it, so the name in a docstring points at whoever holds it now rather
than at the session that wrote the line.

What the finding WAS survives; who found it does not. A comment saying a sweep
looked for the key `"error"` and this one is called `details_error` tells the
next reader why the gate is a function rather than a search. The same comment
naming the session that ran the sweep tells them nothing they can act on.

TWO rules, because an attribution survives losing its name. Keying on the
identity alone leaves the same sentence with the name taken out reading as
ordinary prose: of the five attributions this tree carried, four spell an
identity and the fifth credits the review round instead, and a name-only rule
reds four of five. So the second rule takes a crediting verb whose object is a
person or a review round.

What it must NOT take is the same verb with a MECHANISM as its object. Sixteen
lines in this tree say where a behaviour comes from -- an interpreter, a
helper function, a test, a comparison -- and every one of them is right to.
The object is the whole distinction, and a rule that reds those is worse than
the gap it closes, because the next person to meet a false red widens the
exemption rather than the prose. Both halves are held in the controls below,
in the wordings the tree actually uses, so widening either pattern has to face
the sentences it would start refusing.

Matched against each comment or docstring FLATTENED to one string, not line by
line: prose wraps where the column runs out, and a two-word crediting phrase
wraps with it.

Read through `tokenize` and `ast`, so an identity inside an ordinary string
literal is not prose. `test_the_commit_check_says_why_it_cannot_answer` puts an
identity in a push URL to prove the userinfo is stripped from a job log, and a
line-based sweep would have to exempt the one file whose subject is keeping
that name out of output.

This module's own prose is swept like any other. A path exemption for the file
that defines the rule is the obvious way to let it describe itself, and it
would be an exemption no later addition to the file has to justify -- so the
shapes are spelled in the pattern and in the controls, which are code.
"""

import re
from pathlib import Path

import pytest

from tests.repo.test_source_carries_no_history import prose_in
from tests.repo.test_source_carries_no_history import source_files as history_files

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# Two shapes, because an attribution survives losing its identity. The first is
# the name itself; the second is the crediting phrase with a person-shaped
# object -- what a crediting sentence still reads as once the name is gone.
#
# A session identity, as claimed on this machine: each alternative ends on a
# word boundary, so `dev-dependencies` is not a coordinator and `worker-thread`
# is not a worker.
IDENTITY = re.compile(
    r"\b(?:claude-\d+|antigravity-\d+|worker-[a-z]\b|dev-[0-9a-f]{2,}\b)",
)

# A crediting verb whose object is a person or a review round. The object is
# what decides: "found by the interpreter", "reported by `missing_distributions`"
# and "found by a test" name a mechanism and are how source correctly describes
# where a behaviour comes from. Sixteen such lines are in this tree and none may
# red -- `test_CONTROL_an_attribution_to_a_mechanism_is_not_caught` holds a
# sample of them, because a guard that reds on correct prose gets weakened.
_VERBS = r"(?:found|reported|caught|spotted|noticed|suggested|discovered|raised|flagged)"
ATTRIBUTION = re.compile(
    rf"\b{_VERBS}\s+(?:in\s+review\b|by\s+(?:me|review|a\s+reviewer|the\s+reviewer)\b)",
    re.IGNORECASE,
)

RULES = {"a session identity": IDENTITY, "an attribution to a person": ATTRIBUTION}


def source_files() -> list[Path]:
    """Every Python file in the trees this sweeps: the history sweep's list.

    That list reaches `packages/*/tests`, where a test docstring can say who
    found the defect as easily as anywhere else. The reach is asserted below
    rather than assumed, so narrowing the history sweep reds this one by name.
    """
    return list(history_files())


def attributions_in_prose(paths: list[Path] | None = None) -> list[str]:
    """Return `path:line [rule] ...text...` for every match in prose.

    Each comment or docstring is matched as ONE flattened string rather than
    line by line. A docstring wraps where the column runs out, so a two-word
    crediting phrase arrives split across the break as often as not, and a
    per-line sweep sees neither half of it.
    """
    found: list[str] = []
    for path in paths if paths is not None else source_files():
        rel = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)
        for lineno, text in prose_in(path.read_text()):
            flat = " ".join(text.split())
            for label, pattern in RULES.items():
                for match in pattern.finditer(flat):
                    window = flat[max(0, match.start() - 30) : match.end() + 50]
                    found.append(f"{rel}:{lineno} [{label}] ...{window}...")
    return found


def test_no_source_comment_or_docstring_credits_a_person():
    found = attributions_in_prose()
    assert not found, (
        "source prose credits a person or a review round with a finding. Say "
        "what the finding WAS instead -- the shape of the mistake is what a "
        "reader can use, and an identity is reclaimed by the next session. The "
        "attribution itself belongs in the pull request:\n  " + "\n  ".join(found)
    )


def test_the_sweep_reads_every_tree_including_package_tests():
    """A sweep that lost files would pass the check above by not looking."""
    files = source_files()
    assert set(history_files()) <= set(files), "the history sweep reaches files this one does not"
    reached = {p.relative_to(REPO).as_posix() for p in files}
    for root in ("src/", "tests/", "packages/"):
        assert any(r.startswith(root) for r in reached), f"{root} not reached"
    assert any(re.match(r"packages/[^/]+/tests/", r) for r in reached), (
        "no packages/*/tests file reached"
    )


def test_CONTROL_an_attribution_in_a_docstring_is_caught(tmp_path: Path):
    """The shape this exists for, in the words it was actually written in."""
    path = tmp_path / "sample.py"
    path.write_text(
        "def f():\n"
        '    """Refuse the argument where it was passed.\n\n'
        "    Found in review by claude-3, who went looking for a mutation that\n"
        "    reds this test and not the count.\n"
        '    """\n'
        "    return 1\n"
    )
    found = attributions_in_prose([path])
    assert found, "an attribution in a docstring was not caught"
    # The RULE, not the name. Asserting the name passes with `IDENTITY` removed
    # from `RULES`: the attribution rule matches this same line, and the report
    # window reaches 50 characters past the phrase it matched -- far enough to
    # swallow the identity that follows. The label is what says which rule
    # answered, and this control is the one a reader points at when asking
    # whether the identity rule works.
    assert any("[a session identity]" in f for f in found), found


def test_CONTROL_an_attribution_in_a_comment_is_caught(tmp_path: Path):
    path = tmp_path / "sample.py"
    path.write_text("# dev-cd asked for this to be folded in\nX = 1\n")
    assert attributions_in_prose([path]), "an attribution in a comment was not caught"


def test_CONTROL_an_identity_in_a_string_literal_is_not_prose(tmp_path: Path):
    """The credential-redaction guard's own fixture, reduced.

    It puts an identity in a push URL and asserts the URL never reaches the job
    log. Reading raw lines would red the one file whose subject is keeping that
    name out of output.
    """
    path = tmp_path / "sample.py"
    path.write_text(
        'URL = "https://claude-1:s3cr3t-token@forge.example/org/repo.git"\n'
        'def test_it(result):\n    assert "claude-1" not in result.stderr\n'
    )
    assert attributions_in_prose([path]) == [], "a string literal was read as prose"


def test_CONTROL_the_credential_guard_itself_stays_green():
    """And the real file, not only a reduction of it."""
    path = REPO / "tests" / "repo" / "test_the_commit_check_says_why_it_cannot_answer.py"
    assert path.is_file(), f"{path} is missing; this control is not reading anything"
    assert "claude-1" in path.read_text(), (
        "the credential guard no longer carries an identity, so this control "
        "no longer distinguishes prose from a string literal"
    )
    assert attributions_in_prose([path]) == [], attributions_in_prose([path])


@pytest.mark.parametrize(
    "text",
    [
        "# a dev-dependencies entry is not a coordinator\n",
        "# the worker-thread pool is not a session\n",
        "# claude-code is the product, not an identity\n",
        "# claude-opus-5 is a model id\n",
    ],
)
def test_CONTROL_a_near_miss_is_not_an_identity(tmp_path: Path, text: str):
    """Each of these was a real false positive or one step away from being one."""
    path = tmp_path / "sample.py"
    path.write_text(text + "X = 1\n")
    assert attributions_in_prose([path]) == [], f"{text.strip()!r} matched"


MECHANISM_PROSE = (
    "A console script is found by the interpreter that runs the test, not by PATH.",
    "Undeclared distributions are reported by `missing_distributions`.",
    "Both were found by running two suites against one broker.",
    "Not one of them was caught by anything, because nothing read the documentation.",
    "The same source is flagged by this reader and missed by a literal-only one.",
    "It was found by a test, not by reading.",
    "Found by comparing the two encodings per type rather than trusting one.",
    "The failure would be discovered by a health check that passes while reading.",
    "A `TypeError` raised by building the adapter itself still reaches the caller.",
    "Every entry in SKIP is discovered by the runner.",
)


@pytest.mark.parametrize("sentence", MECHANISM_PROSE, ids=range(len(MECHANISM_PROSE)))
def test_CONTROL_an_attribution_to_a_mechanism_is_not_caught(tmp_path: Path, sentence: str):
    """Taken verbatim from this tree, where each is correct and must stay.

    These are the half the report of this gap warned a loose rule would break:
    lines saying where a behaviour comes from, each naming a mechanism. Kept
    here rather than counted, so widening either pattern has to face the
    sentences it would start refusing.
    """
    path = tmp_path / "sample.py"
    path.write_text(f'def f():\n    """{sentence}"""\n    return 1\n')
    assert attributions_in_prose([path]) == [], f"{sentence!r} was caught"


@pytest.mark.parametrize(
    "prose",
    [
        "Found in review.",
        "Found by review, not by me: the sweep looked for the key `error`.",
        "Found in review of this PR, where a prefixed subject crosses the cap.",
        "The case was spotted by a reviewer reading the prose.",
    ],
    ids=["bare", "not-by-me", "of-this-pr", "by-a-reviewer"],
)
def test_CONTROL_an_attribution_with_no_identity_is_still_caught(tmp_path: Path, prose: str):
    """The half a name-only rule misses, in the wordings this tree actually used."""
    path = tmp_path / "sample.py"
    path.write_text(f'def f():\n    """Do the thing.\n\n    {prose}\n    """\n    return 1\n')
    assert attributions_in_prose([path]), f"{prose!r} was not caught"


def test_CONTROL_a_wrapped_attribution_is_caught(tmp_path: Path):
    """Prose wraps where the column runs out, and a crediting phrase wraps with it."""
    path = tmp_path / "sample.py"
    path.write_text(
        'def f():\n    """Do the thing.\n\n    Found in\n    review.\n    """\n    return 1\n'
    )
    assert attributions_in_prose([path]), "an attribution split across lines was not caught"

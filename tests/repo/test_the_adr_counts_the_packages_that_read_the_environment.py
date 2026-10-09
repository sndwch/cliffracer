"""The ADR sentence about environment variables names each package that reads one and counts the rest.

`docs/decisions.md` says which packages read the process environment and that "the other N packages
read none". It carried the count of a package set that has since grown, with nothing to say so: the
sentence is a claim about every package, and a package added later changes it without touching the
file that states it.

What counts as reading the environment is the three names the sentence itself says it searched for:
`os.environ`, `getenv` and `env_prefix`, in `packages/*/src`. Core is counted apart, as the sentence
does, so the number is over the packages under `packages/`.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
DECISIONS = REPO / "docs" / "decisions.md"

READS_THE_ENVIRONMENT = re.compile(r"os\.environ|getenv|env_prefix")
NUMBER_WORDS = {
    word: number
    for number, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve".split()
    )
}
THE_SENTENCE = re.compile(r"the other (\w+) packages read none")


def packages_and_readers(packages: Path) -> tuple[list[str], list[str]]:
    """Every directory under `packages`, and those whose `src` names the environment."""
    every = sorted(path for path in packages.iterdir() if path.is_dir())
    readers = [
        path.name
        for path in every
        if any(READS_THE_ENVIRONMENT.search(f.read_text()) for f in (path / "src").rglob("*.py"))
    ]
    return [path.name for path in every], readers


def the_paragraph(decisions: str) -> str:
    """The ADR's `Environment variables.` paragraph."""
    start = decisions.index("**Environment variables.**")
    end = decisions.find("\n\n", start)
    return decisions[start : end if end != -1 else None]


def what_the_paragraph_gets_wrong(
    paragraph: str, packages: list[str], readers: list[str]
) -> list[str]:
    """What the paragraph says about the packages that the tree does not bear out."""
    wrong = []
    for reader in readers:
        short = reader.removeprefix("cliffracer-")
        if short not in paragraph:
            wrong.append(f"`{reader}` reads the environment and the paragraph does not name it")
    found = THE_SENTENCE.search(paragraph)
    if found is None:
        return [*wrong, "the paragraph has no 'the other N packages read none' sentence"]
    stated = NUMBER_WORDS.get(found.group(1))
    actual = len(packages) - len(readers)
    if stated != actual:
        wrong.append(
            f"it says the other {found.group(1)} packages read none; the tree has {actual}"
        )
    return wrong


def test_the_adr_paragraph_matches_the_packages():
    packages, readers = packages_and_readers(REPO / "packages")
    # Without these the check below is a loop over nothing: a path that moved reads as agreement.
    assert len(packages) >= 9, f"found only {packages}"
    assert {"cliffracer-cyanide", "cliffracer-logging"} <= set(readers), readers
    assert (
        what_the_paragraph_gets_wrong(the_paragraph(DECISIONS.read_text()), packages, readers) == []
    )


def test_CONTROL_a_stale_count_is_found():
    """The sentence as it read when it was wrong: five, over a tree where cyanide and logging read."""
    packages = [
        f"cliffracer-{n}" for n in "auth cron cyanide kv logging metrics otel resilience".split()
    ]
    readers = ["cliffracer-cyanide", "cliffracer-logging"]
    paragraph = (
        "**Environment variables.** the cyanide extension reads `CLIFFRACER_CYANIDE_*`; the logging "
        "extension reads `CLIFFRACER_LOG_DIR`; the other five packages read none (searching)."
    )
    wrong = what_the_paragraph_gets_wrong(paragraph, packages, readers)
    assert wrong == ["it says the other five packages read none; the tree has 6"]


def test_CONTROL_a_package_the_paragraph_leaves_out_is_found():
    """The one left out is the LAST reader, so a check that stops at the first would pass."""
    packages = ["cliffracer-a", "cliffracer-cyanide", "cliffracer-dlq"]
    readers = ["cliffracer-cyanide", "cliffracer-dlq"]
    paragraph = "**Environment variables.** cyanide reads some; the other one packages read none."
    wrong = what_the_paragraph_gets_wrong(paragraph, packages, readers)
    assert wrong == ["`cliffracer-dlq` reads the environment and the paragraph does not name it"]


def test_CONTROL_a_paragraph_with_no_count_is_found():
    wrong = what_the_paragraph_gets_wrong("**Environment variables.** Core reads two.", ["a"], [])
    assert wrong == ["the paragraph has no 'the other N packages read none' sentence"]


def test_CONTROL_the_paragraph_is_cut_at_its_blank_line():
    text = "**Environment variables.** one.\n\nthe other nine packages read none"
    assert the_paragraph(text) == "**Environment variables.** one."

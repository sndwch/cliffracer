"""Every constant `NumericBounds` and `StringLimits` declare is read by something.

Most of the class surface was unreferenced residue: SQL identifier limits from a persistence
layer this framework does not have, defaults nothing consulted. A file that is mostly unread
constants is one where a reader cannot tell which numbers are load-bearing, which is how
`MAX_TIMEOUT_MS = 3600000` sat beside a function that could not produce it.

A constant counts as read when its name appears in `src/` or a package's `src/` outside its own
declaration line. A name added without a reader fails here, naming it.
"""

import re
from pathlib import Path

import pytest

from cliffracer.core.validation import NumericBounds, StringLimits

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


def _sources() -> list[Path]:
    return sorted([*(REPO / "src").rglob("*.py"), *REPO.glob("packages/*/src/**/*.py")])


def _constants(owner: type) -> list[str]:
    return sorted(name for name in vars(owner) if name.isupper() and not name.startswith("_"))


def _reads(name: str) -> int:
    pattern = re.compile(rf"\b{name}\b")
    declaration = re.compile(rf"^\s*{name}\s*=")
    return sum(
        1
        for path in _sources()
        for line in path.read_text().splitlines()
        if pattern.search(line) and not declaration.match(line)
    )


def test_no_declared_bound_goes_unread():
    unread = {
        f"{owner.__name__}.{name}": _reads(name)
        for owner in (NumericBounds, StringLimits)
        for name in _constants(owner)
        if _reads(name) == 0
    }

    assert not unread, f"declared and never read, so delete them or use them: {sorted(unread)}"


def test_the_sweep_reads_the_classes_and_the_tree():
    """A positive reading, so finding no constants or no sources cannot pass the test above."""
    names = _constants(NumericBounds) + _constants(StringLimits)

    assert len(names) >= 6, names
    assert len(_sources()) > 50
    assert _reads("MAX_CONCURRENT") >= 1


def test_CONTROL_a_name_nothing_reads_is_reported_as_unread():
    assert _reads("A_BOUND_NOBODY_DECLARED_OR_READ") == 0

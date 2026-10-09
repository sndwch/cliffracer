"""Tests ensuring tracked markdown documentation contains no emoji characters."""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


# Complete Unicode emoji and pictograph ranges. Arrows are excluded as valid typography.
EMOJI = re.compile(
    "["
    "\U0001f300-\U0001faff"  # Miscellaneous Symbols and Pictographs, Emoticons, Transport
    "\U0001f000-\U0001f0ff"  # Mahjong, Dominoes, Playing Cards
    "\U0001f100-\U0001f1ff"  # Enclosed Alphanumeric Supplement
    "\U0001f200-\U0001f2ff"  # Enclosed Ideographic Supplement
    "\u2600-\u26ff"  # Miscellaneous Symbols
    "\u2700-\u27bf"  # Dingbats
    "\u2300-\u23ff"  # Miscellaneous Technical (clock, watch, hourglass, media controls)
    "\u25a0-\u25ff"  # Geometric Shapes (play, stop, squares)
    "\u20e3"  # Combining Enclosing Keycap
    "\u2139"  # Information source
    "\u203c"  # Double exclamation
    "\u2049"  # Exclamation question mark
    "\u2122"  # Trade mark
    "\u3030"  # Wavy dash
    "\u303d"  # Part alternation mark
    "\u3297\u3299"  # Circled ideographs
    "⬛⬜⭐⭕"
    "]"
)


def tracked_markdown() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return out


def emoji_lines(paths=None) -> list[str]:
    found = []
    for rel in paths if paths is not None else tracked_markdown():
        doc = rel if isinstance(rel, Path) else REPO / rel
        name = rel if isinstance(rel, str) else doc.name
        for number, line in enumerate(doc.read_text().splitlines(), 1):
            hits = EMOJI.findall(line)
            if hits:
                found.append(f"{name}:{number} [{''.join(hits)}] {line.strip()[:80]}")
    return found


def test_the_sweep_reads_the_documentation():
    """Verify tracked documentation files are discovered."""
    docs = tracked_markdown()
    assert len(docs) >= 15, f"only found {len(docs)} tracked .md files"
    assert all((REPO / d).exists() for d in docs), "git listed a file that is not on disk"


def test_no_document_carries_emoji():
    found = emoji_lines()
    assert not found, (
        "documentation is written in words. Remove these, do not replace them "
        "with a different picture:\n  " + "\n  ".join(found)
    )


@pytest.mark.parametrize(
    "line",
    [
        "## Quick Start 🚀",
        "- ✅ Works",
        "❌ Do not do this",
        "Status: ⚠ deprecated",
        "🤦‍♂️ a ZWJ sequence",
        "Duration: ⏰ 10ms",
        "Watch: ⌚",
        "Loading ⏳ please wait",
        "Play ▶ sample",
        "Square ◼ icon",
        "Keycap: 1️⃣ step",
        "Note ℹ info",
        "Warning ‼ critical",
    ],
)
def test_CONTROL_the_detector_catches_an_emoji(tmp_path: Path, line: str):
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    assert emoji_lines([doc]), f"not caught: {line!r}"


@pytest.mark.parametrize(
    "line",
    [
        "`ServiceConfig(name=...)` → the service name.",
        "| `v3` | → | `v4` |",
        "The arrow in `GET /health → {...}` is typography, not decoration.",
        "A left arrow ← is typography too.",
        "Use `@rpc`, not `@service.rpc`.",
        "Backticks, asterisks and em dashes -- none of it is a pictograph.",
    ],
)
def test_CONTROL_typography_and_prose_are_not_flagged(tmp_path: Path, line: str):
    """Verify standard typography and arrows are not flagged as emoji."""
    doc = tmp_path / "d.md"
    doc.write_text(line + "\n")
    assert not emoji_lines([doc]), f"wrongly flagged: {line!r}"

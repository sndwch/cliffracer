"""The README names every dispatch kind the timing line can start with, and counts them right.

`LoggingExtension` times whatever runs through the hook chain, so the first word of a timing line
is the `kind` of the `WorkerContext` the dispatcher built. The README listed four and the
dispatcher builds five. The kinds are read from the source, so a new dispatch path that builds a
context under a new kind has to be listed.
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
README = REPO / "packages" / "cliffracer-logging" / "README.md"
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}


def dispatch_kinds(root: Path = REPO / "src" / "cliffracer") -> set[str]:
    """Every literal `kind=` handed to `WorkerContext(...)` under the library's source."""
    kinds: set[str] = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "WorkerContext"
            ):
                kinds |= {
                    kw.value.value
                    for kw in node.keywords
                    if kw.arg == "kind"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                }
    return kinds


def readme_kinds(text: str) -> tuple[int | None, set[str]]:
    """The number the README says and the kinds it names, from the 'What timing covers' sentence."""
    sentence = re.search(r"(\w+) kinds reach it\s+today.*?\.\n\n", text, re.DOTALL)
    assert sentence, "the sentence that lists the kinds moved; this guard cannot read it"
    counted = NUMBER_WORDS.get(sentence.group(1).lower())
    return counted, set(re.findall(r"`(\w+)`", sentence.group(0)))


def test_the_readme_names_every_kind_the_dispatcher_builds_a_context_for():
    kinds = dispatch_kinds()
    assert {"rpc", "event", "timer", "describe", "async_rpc"} <= kinds, kinds

    counted, named = readme_kinds(README.read_text())

    assert kinds <= named, f"the README does not list {sorted(kinds - named)}"
    assert counted == len(kinds), (counted, sorted(kinds))


def test_CONTROL_a_readme_missing_a_kind_or_miscounting_one_is_caught():
    four = "Four kinds reach it\ntoday, and each appears first: `rpc`, `async_rpc`, `event` and `timer`.\n\nNext."
    five = four.replace("Four", "Five").replace("`async_rpc`,", "`async_rpc`, `describe`,")

    assert readme_kinds(four) == (4, {"rpc", "async_rpc", "event", "timer"})
    assert readme_kinds(five) == (5, {"rpc", "async_rpc", "describe", "event", "timer"})
    assert not {"rpc", "describe"} <= readme_kinds(four)[1]
    assert readme_kinds(five.replace("Five", "Four"))[0] != len(readme_kinds(five)[1])

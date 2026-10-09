"""Verify docs/extensions.md quotes its worked example verbatim.

Performs a two-way string comparison between the fenced block in docs/extensions.md
and the marked region in tests/integration/test_extensions_guide.py without requiring
a running NATS broker.
"""

import difflib
import re

import pytest

from tests.integration.test_extensions_guide import BEGIN, DOC, END, REPO

pytestmark = pytest.mark.repo

SOURCE = REPO / "tests" / "integration" / "test_extensions_guide.py"
PREAMBLE = "from cliffracer import Extension, RejectMessage, WorkerContext"


def get_worked_example() -> str:
    """Extract the verbatim worked example from the integration test file."""
    src = SOURCE.read_text()
    assert BEGIN in src and END in src, f"the markers in {SOURCE.name} are gone"
    return src.split(BEGIN, 1)[1].split(END, 1)[0].strip("\n")


def extract_guide_example(doc_text: str) -> str | None:
    """Extract the worked example body from docs/extensions.md, stripping the preamble."""
    fences: list[str] = re.findall(r"^```python\s*$(.*?)^```\s*$", doc_text, re.S | re.M)
    for fence in fences:
        fence_clean = fence.strip("\n")
        if fence_clean.startswith(PREAMBLE):
            return str(fence_clean[len(PREAMBLE) :].strip("\n"))
    return None


def guide_problems(example: str, doc_text: str) -> list[str]:
    """What is wrong with how a guide quotes the worked example, one entry each; none when it is exact.

    The one comparison, which the guard below and every CONTROL call: a CONTROL that compared its
    own copy would stay green if this were weakened to a substring test or dropped.
    """
    guide_example = extract_guide_example(doc_text)
    if guide_example is None:
        return [f"the guide has no python fence starting with the required preamble:\n{PREAMBLE}"]
    if guide_example != example:
        diff = "\n".join(
            difflib.unified_diff(
                example.splitlines(),
                guide_example.splitlines(),
                fromfile="the worked example (markers)",
                tofile="the guide (fenced block)",
                lineterm="",
            )
        )
        return [f"the guide does not quote the worked example verbatim:\n{diff}"]
    return []


def test_the_guide_quotes_the_worked_example_verbatim():
    """Byte-for-byte exact equality between the worked example and the guide fence."""
    example = get_worked_example()
    assert example.startswith("class AuditExtension"), example[:60]

    problems = guide_problems(example, DOC.read_text())
    assert not problems, (
        f"{DOC.name}: {problems[0]}\n\n"
        f"Copy the region between markers in {SOURCE.name} into the guide's fenced block."
    )


# The CONTROLs below build their own guide text instead of reading the live
# files. Taking the example from the live source and looking it up in the live
# guide assumes the two already match, which is what the guard above checks: when
# they drift, that lookup finds nothing, and a CONTROL fails beside the guard as
# if the comparison itself were broken. A synthetic guide keeps each CONTROL
# about the comparison alone, whatever state the real copies are in.

SYNTHETIC_BODY = (
    "class AuditExtension(Extension):\n"
    "    async def worker_setup(self, ctx: WorkerContext) -> None:\n"
    '        ctx.data["audited"] = True\n'
    "\n"
    "    async def worker_result(self, ctx: WorkerContext, result, exc) -> None:\n"
    "        pass"
)


def _guide(body: str) -> str:
    """A guide with an unrelated python fence first, then the preamble fence around `body`."""
    return (
        "# Guide\n\n"
        '```python\nprint("an unrelated example")\n```\n\n'
        f"```python\n{PREAMBLE}\n\n{body}\n```\n\n"
        "More prose.\n"
    )


def test_CONTROL_the_reader_finds_the_preamble_fence_in_a_guide():
    """What the CONTROLs below compare is read out of the fence, not the whole text."""
    assert extract_guide_example(_guide(SYNTHETIC_BODY)) == SYNTHETIC_BODY


def test_CONTROL_an_exact_quotation_has_no_problems():
    assert guide_problems(SYNTHETIC_BODY, _guide(SYNTHETIC_BODY)) == []


def test_CONTROL_the_comparison_fails_on_deletion():
    """Removing a line from the guide block fails the comparison the guard makes."""
    lines = SYNTHETIC_BODY.splitlines()
    damaged = "\n".join(lines[:1] + lines[2:])

    assert guide_problems(SYNTHETIC_BODY, _guide(damaged))


def test_CONTROL_the_comparison_fails_on_addition():
    """Appending an unverified hook to the guide block fails it too.

    The direction a substring test does not catch: the example is still inside the guide's block.
    """
    addition = "\n\n    def on_every_message(self, msg) -> None:\n        pass"

    assert guide_problems(SYNTHETIC_BODY, _guide(SYNTHETIC_BODY + addition))


def test_CONTROL_a_guide_without_the_preamble_fence_fails():
    guide = "# Guide\n\n```python\nprint('an unrelated example')\n```\n"

    assert guide_problems(SYNTHETIC_BODY, guide)

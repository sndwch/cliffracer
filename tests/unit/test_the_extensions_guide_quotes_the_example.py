"""Verify docs/extensions.md quotes its worked example verbatim.

Performs a string comparison between the fenced block in docs/extensions.md and
the marked region in tests/integration/test_extensions_guide.py without requiring
a running NATS broker.
"""

import re

import pytest

from tests.integration.test_extensions_guide import BEGIN, DOC, END, REPO

SOURCE = REPO / "tests" / "integration" / "test_extensions_guide.py"


@pytest.mark.unit
def test_the_guide_quotes_the_worked_example_verbatim():
    """Byte-for-byte, not "mentions AuditExtension": a fenced block edited to
    claim something the code does not do is exactly what this catches, and a
    substring-of-a-name check would not."""
    src = SOURCE.read_text()
    assert BEGIN in src and END in src, f"the markers in {SOURCE.name} are gone"
    example = src.split(BEGIN, 1)[1].split(END, 1)[0].strip("\n")
    assert example.startswith("class AuditExtension"), example[:60]

    doc = DOC.read_text()
    fences = re.findall(r"^```python\s*$(.*?)^```\s*$", doc, re.S | re.M)
    assert fences, f"{DOC.name} has no python fences at all"
    assert any(example in f for f in fences), (
        f"{DOC.name} no longer quotes the worked example verbatim. Copy the "
        f"region between the markers in {SOURCE.name} into the guide's fenced "
        "block."
    )


@pytest.mark.unit
def test_CONTROL_the_comparison_can_fail(tmp_path):
    """A check that cannot fail is the same as no check.

    Against a COPY of the guide with one line removed from the quoted block --
    never the tracked file, because an edit-and-restore leaves the repository
    broken if the run is killed, and this train has had runs killed by hand.
    """
    src = SOURCE.read_text()
    example = src.split(BEGIN, 1)[1].split(END, 1)[0].strip("\n")

    doc = DOC.read_text()
    damaged = doc.replace(example.splitlines()[1], "", 1)
    assert damaged != doc, "the mutation changed nothing, so it proves nothing"

    copy = tmp_path / "extensions.md"
    copy.write_text(damaged)
    fences = re.findall(r"^```python\s*$(.*?)^```\s*$", copy.read_text(), re.S | re.M)
    assert not any(example in f for f in fences), "a damaged guide still matched"

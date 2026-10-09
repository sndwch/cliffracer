"""The API reference documents every `StreamSpec` field and the leading-wildcard rule.

`StreamSpec` is what a service declares to get a stream, and its defaults decide
what the stream holds: file storage, limits retention, no age limit and no size
limit. Those facts, and the rule that a stream subject must not begin with a
wildcard (the one that decides whether a cross-namespace listener works against
a declared stream), were written only in the class docstring.

This reads the "Stream fields" section of `docs/api-reference.md`, which lists
each field as a bullet, so a field added to the model without a line there fails
here, and the guard also fails if it stops finding the section it reads.
"""

import re
from pathlib import Path

import pytest

from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.repo

DOC = Path(__file__).resolve().parents[2] / "docs" / "api-reference.md"


def _section(heading: str) -> str:
    """The text under a `###` heading, up to the next heading of any level."""
    text = DOC.read_text()
    match = re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^#{{1,3}} )", text, re.S | re.M)
    return match.group(1) if match else ""


def _documented_fields(section: str) -> set[str]:
    return set(re.findall(r"^- `(\w+)`:", section, re.M))


def test_every_stream_spec_field_has_a_line_in_the_stream_fields_section():
    documented = _documented_fields(_section("Stream fields"))

    missing = sorted(set(StreamSpec.model_fields) - documented)

    assert not missing, (
        f"StreamSpec fields with no bullet under '### Stream fields' in docs/api-reference.md: "
        f"{missing}"
    )


def test_the_section_that_was_read_documents_the_fields_that_exist():
    """A positive reading, so an empty section cannot pass the test above by listing nothing
    to compare, and a bullet for a field that is gone does not stay."""
    documented = _documented_fields(_section("Stream fields"))

    assert len(documented) >= 6, documented
    assert documented <= set(StreamSpec.model_fields), sorted(
        documented - set(StreamSpec.model_fields)
    )


def test_the_defaults_the_section_states_are_the_defaults_the_model_has():
    section = _section("Stream fields")
    fields = StreamSpec.model_fields

    assert fields["storage"].default == "file" and '`"file"` (the default)' in section
    assert fields["retention"].default == "limits" and '`"limits"` (the default)' in section
    assert fields["max_age_seconds"].default is None and "`None` (the default)" in section
    assert fields["duplicate_window_seconds"].default == 120.0 and "`120.0` seconds" in section


def test_the_leading_wildcard_rule_has_a_section():
    section = _section("A stream subject must not begin with a wildcard")

    assert "cross_namespace=True" in section
    assert "err_code 10052" in section

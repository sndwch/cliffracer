"""docs/dead-letters.md names every field a delivery adds to a record, and every header it withholds.

The page tells an operator which fields to read and which headers will never be there. Both lists
are in the publisher, so the page is read against them: a field or a credential name added in code
and not written on the page fails here, naming it.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.credentials import CREDENTIAL_FRAGMENTS, CREDENTIAL_NAMES
from cliffracer.core.dispatch.dlq import DeadLetterPublisher

pytestmark = pytest.mark.repo

PAGE = Path(__file__).resolve().parents[2] / "docs" / "dead-letters.md"


def _delivery_fields() -> set[str]:
    publisher = DeadLetterPublisher(ServiceConfig(name="orders"), lambda: None)
    msg = SimpleNamespace(
        headers={"Authorization": "x", "X-Trace": "t"},
        metadata=SimpleNamespace(
            stream="EVENTS", consumer="c", sequence=SimpleNamespace(stream=1, consumer=1)
        ),
    )
    fields, _ = publisher.origin(msg)
    return set(fields)


def test_every_field_a_delivery_adds_is_named_on_the_page():
    page = PAGE.read_text()
    fields = _delivery_fields()

    assert fields >= {
        "stream",
        "stream_sequence",
        "consumer",
        "original_headers",
        "withheld_headers",
    }
    missing = sorted(field for field in fields if f"`{field}`" not in page)
    assert not missing, f"docs/dead-letters.md does not name {missing}"


def test_every_withheld_header_name_is_named_on_the_page():
    page = PAGE.read_text().lower().replace("_", "-")
    names = {
        *(n.replace("_", "-") for n in CREDENTIAL_NAMES),
        *(f.replace("_", "-") for f in CREDENTIAL_FRAGMENTS),
    }

    missing = sorted(name for name in names if name not in page)
    assert not missing, f"docs/dead-letters.md does not name the withheld header(s) {missing}"


def test_CONTROL_a_field_the_page_does_not_name_is_reported(tmp_path):
    page = "`stream` `stream_sequence`"
    missing = sorted(f for f in _delivery_fields() if f"`{f}`" not in page)

    assert missing == ["consumer", "original_headers", "withheld_headers"]

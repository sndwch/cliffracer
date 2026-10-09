"""ADR-0007 and ADR-0008 quote numbers that belong to the code and to nats-py; they stay equal.

A decision record that says "bounded at 10 seconds" or "pings every 120 seconds" is making a
claim about a constant it does not own. Left unread, the constant moves and the record keeps
saying the old number with the same confidence. This reads the constants where they live, in
`cliffracer.core.connection` and in the installed nats-py, and requires the records to carry them,
and requires `ServiceConfig` to still not expose the ping settings the record says it does not.
"""

import re
from pathlib import Path

import pytest
from nats.aio import client as nats_client

from cliffracer.core.connection import _CLOSED_STOP_TIMEOUT
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


def adr_section(text: str, number: str) -> str:
    match = re.search(
        rf"^## ADR-{number}\b.*?(?=^## ADR-|\Z)", text, flags=re.MULTILINE | re.DOTALL
    )
    assert match, f"ADR-{number} is not in decisions.md"
    return match.group(0)


def _decisions() -> str:
    return (REPO / "docs" / "decisions.md").read_text()


def missing_from_adr_0007(text: str) -> list[str]:
    section = adr_section(text, "0007")
    needed = [f"{int(_CLOSED_STOP_TIMEOUT)} seconds"]
    return [phrase for phrase in needed if phrase not in section]


def missing_from_adr_0008(text: str) -> list[str]:
    section = adr_section(text, "0008")
    needed = [
        f"`ping_interval` {nats_client.DEFAULT_PING_INTERVAL} seconds",
        f"`max_outstanding_pings` {nats_client.DEFAULT_MAX_OUTSTANDING_PINGS}",
        f"{nats_client.DEFAULT_PENDING_SIZE // (1024 * 1024)} MiB `pending_size`",
    ]
    return [phrase for phrase in needed if phrase not in section]


def test_adr_0007_states_the_stop_bound_the_close_callback_uses():
    assert missing_from_adr_0007(_decisions()) == []


def test_adr_0008_states_the_ping_and_buffer_defaults_nats_py_has():
    assert missing_from_adr_0008(_decisions()) == []


def test_the_service_config_leaves_the_ping_defaults_to_nats_py_until_they_are_set():
    """Unset means nats-py's own 120 and 2, which ADR-0008 and the architecture guide quote."""
    fields = ServiceConfig.model_fields

    assert fields["ping_interval"].default is None
    assert fields["max_outstanding_pings"].default is None
    assert "pending_size" not in fields


def silent_partition_window(interval: float, outstanding: int) -> str:
    """The window the documents state, in the words they use, for these two settings."""
    return f"{interval * outstanding:g} to {interval * (outstanding + 1):g}"


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_documents_state_the_silent_partition_window_nats_py_gives_by_default():
    window = silent_partition_window(
        nats_client.DEFAULT_PING_INTERVAL, nats_client.DEFAULT_MAX_OUTSTANDING_PINGS
    )
    guide = _flat((REPO / "docs" / "ARCHITECTURE.md").read_text())
    decision = _flat(adr_section(_decisions(), "0008"))

    assert f"{window} seconds at nats-py's defaults" in guide, window
    assert window.replace(" to ", " and ") in decision, window


def test_CONTROL_a_record_that_quotes_a_different_number_is_reported():
    text = _decisions()

    assert missing_from_adr_0007(text.replace("bounded at 10 seconds", "bounded at 5 seconds")) == [
        "10 seconds"
    ]
    assert missing_from_adr_0008(
        text.replace("`ping_interval` 120 seconds", "`ping_interval` 60 seconds")
    ) == ["`ping_interval` 120 seconds"]


def test_the_documents_that_describe_shutdown_timeout_name_the_cap_a_close_puts_on_it():
    """A terminal close stops the service in a fixed time that `shutdown_timeout` does not govern.

    The field says it bounds the drain; for the stop a closed connection starts that is not
    so, and a reader sizing `shutdown_timeout` for a long drain would otherwise never learn
    that the stop is cut off first.
    """
    needed = f"{int(_CLOSED_STOP_TIMEOUT)} seconds"
    description = ServiceConfig.model_fields["shutdown_timeout"].description or ""
    reference = (REPO / "docs" / "api-reference.md").read_text()
    row = next(line for line in reference.splitlines() if line.startswith("| `shutdown_timeout`"))

    assert needed in description, description
    assert needed in row, row


def missing_the_on_shutdown_ceiling(ceiling: float) -> list[str]:
    """Where the documents fail to state the seconds `on_shutdown` gets when there is no setting.

    A stop cancelled before `on_shutdown` runs it for `shutdown_timeout` seconds, or for a fixed
    ceiling when that is `None`. The number is the code's (`lifecycle.ON_SHUTDOWN_CEILING`), and five
    documents quote it, so each is read for it.
    """
    seconds = f"{ceiling:g}"
    description = ServiceConfig.model_fields["shutdown_timeout"].description or ""
    reference = (REPO / "docs" / "api-reference.md").read_text()
    row = next(line for line in reference.splitlines() if line.startswith("| `shutdown_timeout`"))
    guide = " ".join((REPO / "docs" / "extensions.md").read_text().split())
    adr = " ".join(adr_section(_decisions(), "0007").split())
    wanted = {
        "ADR-0007": (adr, f"a fixed {seconds} seconds when that is `None`"),
        "the shutdown_timeout description": (description, f"({seconds} when this is `None`)"),
        "the configuration reference row": (row, f"({seconds} when this is `None`)"),
        "the extensions guide": (guide, f"a fixed {seconds} seconds when that is `None`"),
    }
    return [place for place, (text, phrase) in wanted.items() if phrase not in text]


def test_the_documents_state_the_ceiling_on_shutdown_gets_when_there_is_no_shutdown_timeout():
    from cliffracer.core.lifecycle import ON_SHUTDOWN_CEILING

    assert missing_the_on_shutdown_ceiling(ON_SHUTDOWN_CEILING) == []


def test_CONTROL_a_different_ceiling_is_reported_in_every_place_that_quotes_it():
    assert len(missing_the_on_shutdown_ceiling(45.0)) == 4


def test_CONTROL_the_window_follows_the_two_settings():
    assert silent_partition_window(120, 2) == "240 to 360"
    assert silent_partition_window(5, 2) == "10 to 15"
    assert silent_partition_window(1, 1) == "1 to 2"


#: The settings the architecture guide works its example for.
EXAMPLE_INTERVAL, EXAMPLE_OUTSTANDING = 5, 2


def test_the_guides_worked_example_states_the_window_its_own_settings_give():
    guide = _flat((REPO / "docs" / "ARCHITECTURE.md").read_text())
    window = silent_partition_window(EXAMPLE_INTERVAL, EXAMPLE_OUTSTANDING)

    assert (
        f"{EXAMPLE_INTERVAL} seconds and {EXAMPLE_OUTSTANDING} narrow it to {window} seconds, at the "
        f"cost of one ping on each connection every {EXAMPLE_INTERVAL} seconds"
    ) in guide


def test_the_documents_state_the_defaults_of_the_broker_probe_settings():
    """The architecture guide, ADR-0008 and the changelog fragment quote the two defaults."""
    fields = ServiceConfig.model_fields
    timeout = fields["broker_probe_timeout"].default
    cache = fields["broker_probe_cache"].default
    guide = _flat((REPO / "docs" / "ARCHITECTURE.md").read_text())
    decision = _flat(adr_section(_decisions(), "0008"))

    assert f"`broker_probe_timeout` ({timeout:g} seconds by default)" in guide
    assert f"`broker_probe_cache` seconds ({cache:g} by default)" in guide
    assert f"`broker_probe_timeout`, {timeout:g} seconds by default" in decision

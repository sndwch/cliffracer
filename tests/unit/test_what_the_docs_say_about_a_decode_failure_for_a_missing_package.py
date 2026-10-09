"""Every document that says a failure while decoding is never redelivered names the exception.

A body in an encoding the service lacks the package to read (msgpack without the `msgpack` extra)
is the service's fault and not the message's, so a JetStream delivery of it is redelivered like a
handler failure. The dead-letter page said so; ADR-0014 and the API reference still said whatever
raises while the payload is decoded terminates the delivery. This reads the three places and runs the
case, so a fourth sentence that says "never redelivered" without the exception is found by the first.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core import validation
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import MockMessage, ServiceTestHarness
from cliffracer.testing.messages import MockJetStreamMetadata

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def _flat(path: str) -> str:
    return " ".join((REPO / path).read_text().split())


def _adr_0014() -> str:
    text = (REPO / "docs" / "decisions.md").read_text()
    start = text.index("## ADR-0014")
    return " ".join(text[start : text.index("\n## ADR-0015")].split())


@pytest.mark.parametrize(
    ("document", "text"),
    [
        pytest.param(
            "docs/dead-letters.md", lambda: _flat("docs/dead-letters.md"), id="dead-letters"
        ),
        pytest.param(
            "docs/api-reference.md", lambda: _flat("docs/api-reference.md"), id="api-reference"
        ),
        pytest.param("ADR-0014", _adr_0014, id="adr-0014"),
    ],
)
def test_each_document_names_the_missing_package_exception(document, text):
    assert "lacks the package to read" in text() or "lacks a package to read" in text(), document


class Consumer(CliffracerService):
    @listener("probe.declared.thing", durable="prober")
    async def on_thing(self, subject: str, value: int = 0) -> None:
        return None


async def test_the_documented_case_is_what_the_dispatcher_does():
    config = ServiceConfig(
        name="consumer_svc",
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="DECLARED", subjects=["probe.declared.>"]),
            StreamSpec(name="DLQ", subjects=["dlq.>"]),
        ],
    )
    async with ServiceTestHarness(Consumer, config=config) as harness:
        msg = MockMessage(
            subject="probe.declared.thing",
            data=b"\x81\xa5value\x01",
            headers={"Content-Type": "application/msgpack"},
            metadata=MockJetStreamMetadata(num_delivered=1),
        )
        with patch.object(validation, "msgpack", None):
            await harness.container.dispatcher.jetstream.handle_jetstream_event(
                msg, pattern="probe.declared.thing"
            )

    assert msg.nacked and not msg.terminated

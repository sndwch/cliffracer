"""One inbound event is one unit of work, so every handler it reaches logs under one id.

An event with no correlation id on the wire and two matching listeners ran each handler under a
different generated id, and nothing tied the two lines to the one message. The id is resolved once
per message, from the message (its headers, then its payload) or new, and each handler's dispatch
carries it. A message that does carry an id is unchanged: every handler already saw it.
"""

import json

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.correlation import correlation_id_var
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

SUBJECT = "things.happened"


def _service(seen: list[tuple[str, str | None]]) -> CliffracerService:
    class Svc(CliffracerService):
        @listener(SUBJECT, fanout=True)
        async def exact(self, subject: str) -> None:
            seen.append(("exact", correlation_id_var.get()))

        @listener("things.*", fanout=True)
        async def wildcard(self, subject: str) -> None:
            seen.append(("wildcard", correlation_id_var.get()))

    return Svc(ServiceConfig(name="fanout", health_port=0))


async def _deliver(headers: dict[str, str], body: dict) -> list[tuple[str, str | None]]:
    seen: list[tuple[str, str | None]] = []
    service = _service(seen)
    await service.container._setup_extensions()
    service._discover_handlers()
    message = MockMessage(subject=SUBJECT, data=json.dumps(body).encode(), headers=headers)

    await service.container.dispatcher.events.handle_event(message)

    return seen


async def test_a_message_with_no_id_gives_both_handlers_the_same_new_one():
    seen = await _deliver({}, {})

    assert sorted(name for name, _ in seen) == ["exact", "wildcard"]
    ids = {cid for _, cid in seen}
    assert len(ids) == 1, seen
    (only,) = ids
    assert only is not None and only.startswith("corr_")


async def test_two_messages_get_two_ids():
    first = await _deliver({}, {})
    second = await _deliver({}, {})

    assert {cid for _, cid in first} != {cid for _, cid in second}


async def test_an_id_on_the_wire_is_the_one_both_handlers_see():
    seen = await _deliver({"X-Correlation-ID": "corr_fromthewire000001"}, {})

    assert {cid for _, cid in seen} == {"corr_fromthewire000001"}


async def test_an_id_in_the_payload_is_the_one_both_handlers_see():
    seen = await _deliver({}, {"correlation_id": "corr_frompayload00001"})

    assert {cid for _, cid in seen} == {"corr_frompayload00001"}

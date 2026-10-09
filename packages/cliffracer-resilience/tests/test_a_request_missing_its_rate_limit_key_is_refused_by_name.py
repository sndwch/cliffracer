"""A message that lacks the value its handler's limit partitions by is refused, naming what is missing.

The extension raised from its own check, which the pipeline reads as the service being broken: the
caller was told `internal error`, an error was logged per request, and a durable event was
redelivered up to its limit and dead-lettered for input no redelivery could fix. Leaving out the
header is the caller's input, and a refusal an extension authors is a decision: an RPC is answered
`refused`, and a durable event is acknowledged.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import ResilienceExtension, rate_limit

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


class Keyed(CliffracerService):
    resilience = ResilienceExtension()

    def __init__(self) -> None:
        super().__init__(
            ServiceConfig(
                name="keyed",
                subject_prefix=None,
                jetstream_enabled=True,
                jetstream_streams=[StreamSpec(name="KEYED", subjects=["keyed.created"])],
            )
        )
        self.handled: list[str] = []

    @rpc
    @rate_limit(calls=5, window=60.0, key="x-client")
    async def by_header(self) -> int:
        return 1

    @rpc
    @rate_limit(calls=5, window=60.0, key="client", key_source="payload")
    async def by_payload(self, client: str = "") -> int:
        return 1

    @listener("keyed.created", durable="keyed_created")
    @rate_limit(calls=5, window=60.0, key="client", key_source="payload")
    async def on_created(self, client: str = "") -> None:
        self.handled.append(client)


async def _started() -> Keyed:
    service = Keyed()
    await service.container._setup_extensions()
    service._discover_handlers()
    return service


async def _call(service: Keyed, method: str, *, headers: dict | None = None, **payload) -> dict:
    message = MockMessage(
        f"keyed.rpc.{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        reply="_INBOX.k",
    )
    await service.container._handle_rpc_request(message)
    assert message.responded_data is not None
    return json.loads(message.responded_data)


def _delivery(payload: dict, *, delivered: int = 1) -> AsyncMock:
    message = AsyncMock()
    message.subject = "keyed.created"
    message.data = json.dumps(payload).encode()
    message.headers = None
    message.metadata = SimpleNamespace(num_delivered=delivered)
    return message


@pytest.mark.parametrize(
    ("method", "payload", "missing"),
    [("by_header", {}, "x-client"), ("by_payload", {}, "client")],
)
async def test_an_rpc_without_the_key_is_refused_and_the_reply_names_the_key(
    method, payload, missing
):
    service = await _started()

    reply = await _call(service, method, **payload)

    assert reply["success"] is False
    assert reply["code"] == "refused", reply
    assert missing in reply["error"] and reply["error"].startswith("refused: "), reply
    assert "internal" not in json.dumps(reply)


async def test_the_refusal_is_counted_as_a_refusal_and_not_as_a_fault():
    service = await _started()

    await _call(service, "by_header")

    assert service.resilience.health_details()["rate_limits"]["by_handler"]["by_header"] == {
        "permitted": 0,
        "refused": 1,
    }


async def test_a_durable_event_without_the_key_is_acknowledged_not_redelivered():
    service = await _started()
    message = _delivery({"other": 1})

    await service.container._handle_jetstream_event(message)

    message.ack.assert_awaited_once()
    message.nak.assert_not_awaited()
    message.term.assert_not_awaited()
    assert service.handled == []


async def test_CONTROL_a_request_with_the_key_is_admitted_and_so_is_an_event():
    service = await _started()

    reply = await _call(service, "by_header", headers={"X-Client": "a"})
    message = _delivery({"client": "a"})
    await service.container._handle_jetstream_event(message)

    assert reply["success"] is True, reply
    message.ack.assert_awaited_once()
    assert service.handled == ["a"]

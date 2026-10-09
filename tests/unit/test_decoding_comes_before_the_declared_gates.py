"""A message that cannot be decoded is refused before any declared extension is consulted.

`docs/extensions.md` promises that a gate a service declares refuses a message before any schema
diagnostic is produced for it. That holds for a payload that decodes and fails its schema. Decoding
is the dispatcher's own first step: an RPC with a body that is not JSON or msgpack is answered
`validation_failed` with the decoder's text (under the default policy), a fire-and-forget request is
logged and dropped, and an event is dead-lettered, each without a declared gate running, so a gate
spends no permit on it and a caller cannot make a gate see it. The order is pinned here so the
documentation and the dispatcher cannot drift apart.
"""

import json
from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import ResilienceExtension

from cliffracer import CliffracerService, ServiceConfig, async_rpc, listener, rpc
from cliffracer.core.container import DispatchOutcome
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit

MALFORMED = b'{"account": "a", "amount": '
WRONG_SCHEMA = json.dumps({"account": "a", "amount": "lots"}).encode()


class Gate(Extension):
    """A refusing gate that records every message it is consulted about."""

    def __init__(self) -> None:
        self.consulted: list[str] = []

    async def worker_setup(self, ctx):
        self.consulted.append(ctx.kind)
        raise RejectMessage("unauthenticated")


class Bank(CliffracerService):
    gate = Gate()

    @rpc
    async def transfer(self, account: str, amount: int) -> str:
        return "moved"

    @async_rpc
    async def audit(self, account: str, amount: int) -> None:
        return None

    @listener("bank.moved", fanout=True)
    async def on_moved(self, account: str = "", amount: int = 0) -> None:
        return None


def _bank() -> Bank:
    bank = Bank(ServiceConfig(name="bank", health_port=0))
    bank._discover_handlers()
    bank.nc = AsyncMock()
    return bank


def _request(subject: str, body: bytes) -> MockMessage:
    return MockMessage(
        subject, data=body, headers={"Content-Type": "application/json"}, reply="_INBOX.r"
    )


async def _rpc(bank: Bank, body: bytes) -> dict:
    msg = _request("bank.rpc.transfer", body)
    await bank.container._handle_rpc_request(msg)
    assert msg.responded_data is not None
    return json.loads(msg.responded_data)


def _gate(bank: Bank) -> Gate:
    return next(e for e in bank.container.extensions if e.name == "gate")


async def test_an_rpc_with_a_wrong_schema_is_refused_by_the_gate():
    bank = _bank()

    reply = await _rpc(bank, WRONG_SCHEMA)

    assert reply["code"] == "refused" and reply["error"] == "refused: unauthenticated", reply
    assert _gate(bank).consulted == ["rpc"]


async def test_an_rpc_that_cannot_be_decoded_never_reaches_the_gate():
    bank = _bank()

    reply = await _rpc(bank, MALFORMED)

    assert reply["code"] == "validation_failed", reply
    assert _gate(bank).consulted == []


async def test_an_async_rpc_with_a_wrong_schema_is_seen_by_the_gate_and_a_malformed_one_is_not():
    bank = _bank()
    await bank.container._handle_async_request(_request("bank.async.audit", WRONG_SCHEMA))
    assert _gate(bank).consulted == ["async_rpc"]

    await bank.container._handle_async_request(_request("bank.async.audit", MALFORMED))

    assert _gate(bank).consulted == ["async_rpc"], "the malformed request reached the gate"


async def test_an_event_that_decodes_is_seen_by_the_gate_and_one_that_does_not_is_not():
    bank = _bank()
    good = MockMessage("bank.moved", data=WRONG_SCHEMA, headers={}, reply=None)
    await bank.container._dispatch_event(good, pattern="bank.moved", raise_on_error=False)
    assert _gate(bank).consulted == ["event"]

    bad = MockMessage("bank.moved", data=MALFORMED, headers={}, reply=None)
    outcome = await bank.container._dispatch_event(bad, pattern="bank.moved", raise_on_error=False)

    assert outcome == DispatchOutcome.INVALID
    assert _gate(bank).consulted == ["event"], "the undecodable event reached the gate"


class Limited(CliffracerService):
    resilience = ResilienceExtension(default_calls=2, default_window=60.0)

    @rpc
    async def transfer(self, account: str, amount: int) -> str:
        return "moved"


async def test_a_limit_is_spent_by_a_payload_that_decodes_and_fails_and_not_by_one_that_does_not():
    bank = Limited(ServiceConfig(name="bank", health_port=0))
    await bank.container._setup_extensions()
    bank._discover_handlers()
    bank.nc = AsyncMock()

    undecodable = [await _rpc(bank, MALFORMED) for _ in range(5)]
    first = await _rpc(bank, WRONG_SCHEMA)
    second = await _rpc(bank, WRONG_SCHEMA)
    third = await _rpc(bank, WRONG_SCHEMA)

    assert {r["code"] for r in undecodable} == {"validation_failed"}, (
        "malformed JSON spent a permit"
    )
    assert (first["code"], second["code"]) == ("validation_failed", "validation_failed")
    assert third["error"] == "refused: rate limit exceeded", third

"""`verify()` runs once per connection, not once per call that races for it.

The module docstring says "It runs once per connection, lazily, on the first
call." `_connection()` guards connecting with a lock; nothing guarded
verifying. `_call` read `self._verified`, awaited `verify()`, and `verify()`
set the flag only after a full round trip -- so every call that started before
the first describe returned saw `False` and sent its own.

MEASURED ON THE UNFIXED CODE, 50 concurrent first calls through the real
`_call` with a 10ms fake round trip:

    50 concurrent first calls -> describe requests sent: 50, rpc: 50

Three consequences, in the issue's words: the documented promise is false under
the concurrency this framework exists for; the fan-out lands exactly when a
process starts and opens its pool; and because describe is served from the RPC
queue group, N describes sample N arbitrary replicas, so during a rolling
deploy concurrent calls can verify against different builds and disagree.

THE TESTS COUNT DESCRIBES, NOT TIME. "How many describes went out" is the thing
the promise is about and it is exact; a duration would measure the host.

WHAT IS DELIBERATELY NOT DEDUPLICATED. `verify()` itself still performs a round
trip every time it is called: it is the public "ask the service to describe
itself", and `_call` re-invokes it on `RpcValidationError` precisely to catch a
replica that moved. Only the lazy first-use path is collapsed.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import ClientOutOfDateError, RpcValidationError

pytestmark = pytest.mark.unit

CONCURRENT = 50

DESCRIPTION = {
    "service": "svc",
    "version": "1",
    "description_hash": "sha256:d",
    "methods": [{"name": "do", "signature_hash": "sha256:x", "params": [], "returns": None}],
}


class Recorder:
    """Counts the subjects a client sends, and answers them."""

    def __init__(self, *, rpc_reply: bytes = b'{"success":true,"result":1}', description=None):
        self.sent: list[str] = []
        self.rpc_reply = rpc_reply
        self.description = DESCRIPTION if description is None else description

    async def request(self, subject, payload, headers=None):
        self.sent.append(subject)
        await asyncio.sleep(0.01)  # a round trip, so calls really do overlap
        if subject.endswith("describe"):
            return SimpleNamespace(data=json.dumps(self.description).encode(), headers=None)
        return SimpleNamespace(data=self.rpc_reply, headers=None)

    @property
    def describes(self) -> int:
        return sum(1 for s in self.sent if s.endswith("describe"))


def _client(recorder: Recorder) -> ServiceClient:
    client = ServiceClient(service="svc")
    client.SIGNATURES = {"do": "sha256:x"}
    client._nc = AsyncMock()
    client._request = recorder.request  # type: ignore[method-assign]
    return client


async def test_fifty_concurrent_first_calls_describe_once():
    """The promise, under the concurrency it is supposed to hold for."""
    recorder = Recorder()
    client = _client(recorder)

    await asyncio.gather(*[client._call("do", {}, int) for _ in range(CONCURRENT)])

    assert recorder.describes == 1, recorder.sent
    assert len(recorder.sent) == CONCURRENT + 1, recorder.sent


async def test_the_one_describe_comes_first_and_nothing_describes_after_it():
    """Verification happens BEFORE any call goes out, not merely once.

    THIS IS NOT A NICER DIAGNOSTIC FOR THE COUNT TEST. It catches a defect the
    count cannot see at all: move `_verify_once()` below the request in `_call`
    and there is still exactly one describe, so
    `test_fifty_concurrent_first_calls_describe_once` passes -- while fifty
    unverified RPCs go out ahead of it:

        AssertionError: ['svc.rpc.do', 'svc.rpc.do', 'svc.rpc.do', ...]

    The client's promise is that it checks the service's signatures before it
    calls, and a count of describes is blind to when the describe happened.
    This assertion is the only thing in the suite that holds that.

    It also tells the two concurrency mutations apart, deterministically and
    without a clock, because their subject ORDERS differ:

        unguarded          describe, describe, describe, describe, ...
        lock, no re-check  describe, rpc, describe, rpc, describe, rpc, ...
        as shipped         describe, rpc, rpc, rpc, rpc, ...

    but that is a side benefit. The reason this test exists separately is a
    mutation that reds it and not the count -- because two assertions that
    always red together invite someone to delete one.
    """
    recorder = Recorder()
    client = _client(recorder)

    await asyncio.gather(*[client._call("do", {}, int) for _ in range(CONCURRENT)])

    assert recorder.sent[0].endswith("describe"), recorder.sent[:4]
    assert not any(s.endswith("describe") for s in recorder.sent[1:]), recorder.sent[:6]


async def test_every_concurrent_call_still_gets_its_answer():
    """Collapsing the describes must not collapse the calls."""
    recorder = Recorder()
    client = _client(recorder)

    results = await asyncio.gather(*[client._call("do", {}, int) for _ in range(CONCURRENT)])

    assert results == [1] * CONCURRENT


async def test_CONTROL_a_second_connection_verifies_again():
    """Once per CONNECTION, not once per client.

    A reconnect clears the flag, because a restarted service can be a different
    build. Without this, "verifies once" would be satisfied by never verifying
    again at all.
    """
    recorder = Recorder()
    client = _client(recorder)

    await client._call("do", {}, int)
    assert recorder.describes == 1

    await client._on_reconnect()
    await client._call("do", {}, int)

    assert recorder.describes == 2, recorder.sent


async def test_CONTROL_later_calls_do_not_describe_again():
    """The ordinary path: one describe, then nothing."""
    recorder = Recorder()
    client = _client(recorder)

    for _ in range(5):
        await client._call("do", {}, int)

    assert recorder.describes == 1, recorder.sent


async def test_calling_verify_directly_always_asks():
    """`verify()` is the public "ask the service"; it is not memoised."""
    recorder = Recorder()
    client = _client(recorder)

    await client.verify()
    await client.verify()

    assert recorder.describes == 2, recorder.sent


async def test_a_validation_error_still_re_verifies():
    """`_call` re-verifies on RpcValidationError to catch a replica that moved.

    That path calls `verify()` directly and must not be skipped by the
    first-use guard, which by then considers the client verified.
    """
    recorder = Recorder(
        rpc_reply=json.dumps(
            {"success": False, "error": "validation failed", "details": []}
        ).encode()
    )
    client = _client(recorder)

    with pytest.raises(RpcValidationError):
        await client._call("do", {}, int)

    assert recorder.describes == 2, recorder.sent


async def test_a_failed_verification_is_not_cached_as_success():
    """A client that is out of date must keep saying so, on every call."""
    drifted = dict(
        DESCRIPTION,
        methods=[{"name": "do", "signature_hash": "sha256:MOVED", "params": [], "returns": None}],
    )
    recorder = Recorder(description=drifted)
    client = _client(recorder)

    for _ in range(3):
        with pytest.raises(ClientOutOfDateError):
            await client._call("do", {}, int)

    assert client._verified is False

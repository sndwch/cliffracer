"""A drift reply re-verifies once for everyone who saw it, not once each.

`_call` re-invokes `verify()` when a reply comes back `RpcValidationError`,
because in a rolling deploy the request may have hit a newer replica. That is
correct and must stay: answering it from `_verified` would disable the drift
detection the branch exists for, since the flag is already True by then.

Nothing shared the answer, though. A batch of in-flight calls that all fail
validation together sent one describe EACH.

MEASURED before the fix, through the real `_call` with a 10ms round trip:

    50 concurrent first calls, all RpcValidationError -> describes: 51
    already verified, then 50 concurrent failing calls -> retry describes: 50

Describe is served from the RPC queue group, so those N requests sample N
arbitrary replicas -- the same fan-out and the same disagreement that the
first-use path was fixed for, on the path a rolling deploy actually causes.

A SINGLE-FLIGHT, NOT A MEMO, and the tests below are shaped around that
difference. A lock would serialise the cohort and still ask N times. A cached
answer would be worse than the bug: the next drift is a NEW fact and must be
asked again. So `test_a_later_drift_asks_again` is the one that fails for the
tempting wrong fix, and it is why the slot is cleared rather than kept.
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
DRIFT_REPLY = json.dumps({"success": False, "error": "validation failed", "details": []}).encode()


class Recorder:
    """Counts and orders the subjects a client sends, and answers them."""

    def __init__(self, *, rpc_reply: bytes = DRIFT_REPLY, description=None) -> None:
        self.sent: list[str] = []
        self.rpc_reply = rpc_reply
        self.description = DESCRIPTION if description is None else description

    async def request(self, subject, payload, headers=None):
        self.sent.append(subject)
        await asyncio.sleep(0.01)  # a round trip, so the calls really do overlap
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


async def _drift_together(client: ServiceClient, count: int = CONCURRENT) -> list[BaseException]:
    out = await asyncio.gather(
        *[client._call("do", {}, int) for _ in range(count)], return_exceptions=True
    )
    return [e for e in out if isinstance(e, BaseException)]


async def test_a_cohort_that_drifts_together_sends_one_describe() -> None:
    """The defect, on the path the rolling deploy causes."""
    recorder = Recorder()
    client = _client(recorder)
    client._verified = True  # already past first use; this is the RETRY path

    raised = await _drift_together(client)

    assert recorder.describes == 1, recorder.sent
    assert len(raised) == CONCURRENT
    assert all(isinstance(e, RpcValidationError) for e in raised), {type(e) for e in raised}


async def test_the_one_describe_comes_after_the_calls_that_caused_it() -> None:
    """The SHAPE, which tells the fixes apart without a clock.

        unshared     rpc, describe, rpc, describe, rpc, describe, ...
        as shipped   rpc, rpc, ... , describe        (one, and not first)

    A count says "one". It does not say the cohort shared it rather than one
    call winning a race while the rest were serialised behind a lock.
    """
    recorder = Recorder()
    client = _client(recorder)
    client._verified = True

    await _drift_together(client)

    assert recorder.sent[0].endswith("rpc.do"), recorder.sent[:4]
    assert recorder.sent.count("svc.describe") == 1, recorder.sent
    # every rpc went out before the describe came back: they overlapped rather
    # than queueing, which a per-call re-verify could not produce
    first_describe = recorder.sent.index("svc.describe")
    assert first_describe >= CONCURRENT - 1, (first_describe, recorder.sent[:6])


async def test_a_later_drift_asks_again() -> None:
    """CONTROL, and the one the tempting wrong fix fails.

    A cached answer would satisfy every other test here and break the thing the
    re-verify is for: a second drift is a NEW fact about the fleet.
    """
    recorder = Recorder()
    client = _client(recorder)
    client._verified = True

    await _drift_together(client, count=3)
    assert recorder.describes == 1, recorder.sent

    await _drift_together(client, count=3)
    assert recorder.describes == 2, recorder.sent


async def test_the_shared_answer_reaches_every_caller() -> None:
    """A client that IS out of date must tell all of them, not just the winner."""
    drifted = dict(
        DESCRIPTION,
        methods=[{"name": "do", "signature_hash": "sha256:MOVED", "params": [], "returns": None}],
    )
    recorder = Recorder(description=drifted)
    client = _client(recorder)
    client._verified = True

    raised = await _drift_together(client, count=5)

    assert recorder.describes == 1, recorder.sent
    assert len(raised) == 5
    assert all(isinstance(e, ClientOutOfDateError) for e in raised), {type(e) for e in raised}


async def test_a_caller_going_away_does_not_cancel_the_cohorts_answer() -> None:
    """CONTROL for the detached task.

    Awaiting a shared task directly would let the first caller's cancellation
    propagate into it and abandon everyone still waiting. The shield is what
    stops that, and nothing else in this file would notice if it went.
    """
    recorder = Recorder()
    client = _client(recorder)
    client._verified = True

    first = asyncio.create_task(client._call("do", {}, int))
    others = [asyncio.create_task(client._call("do", {}, int)) for _ in range(4)]
    await asyncio.sleep(0.015)  # let them all reach the re-verify
    first.cancel()

    settled = await asyncio.gather(first, *others, return_exceptions=True)

    assert isinstance(settled[0], asyncio.CancelledError), settled[0]
    assert all(isinstance(e, RpcValidationError) for e in settled[1:]), [
        type(e) for e in settled[1:]
    ]
    assert recorder.describes == 1, recorder.sent


async def test_CONTROL_the_first_use_path_is_unchanged() -> None:
    """The first-use property: 50 concurrent FIRST calls still describe once."""
    recorder = Recorder(rpc_reply=b'{"success":true,"result":1}')
    client = _client(recorder)

    results = await asyncio.gather(*[client._call("do", {}, int) for _ in range(CONCURRENT)])

    assert recorder.describes == 1, recorder.sent
    assert results == [1] * CONCURRENT


async def test_CONTROL_calling_verify_directly_still_always_asks() -> None:
    """`verify()` is the public "ask the service"; it is not shared either."""
    recorder = Recorder()
    client = _client(recorder)

    await client.verify()
    await client.verify()

    assert recorder.describes == 2, recorder.sent


async def test_a_fully_cancelled_cohort_leaves_no_unretrieved_exception() -> None:
    """CONTROL for the retrieving callback, and the reason it is not decoration.

    `shield` retrieves the inner exception for a cancelled awaiter, but only
    while its callback is registered: cancelling the outer BEFORE the inner
    settles removes it (`_outer_done_callback` in `asyncio.tasks`). So a cohort
    that ALL goes away leaves the re-verify's `ClientOutOfDate` unclaimed, and
    asyncio logs "Task exception was never retrieved" at ERROR from
    `Task.__del__` -- during a rolling deploy, which is when someone is reading
    the log. Correctness is unaffected; the cost is the noise.

    ASSERTED ON `_log_traceback` RATHER THAN ON THE WARNING. The warning fires
    from `__del__`, so observing it needs the task to be collected, and inside a
    pytest loop enough references survive that it does not fire on demand -- a
    version of this test that installed an exception handler and forced `gc`
    passed with the fix REMOVED, which is the failure it was written to catch.
    `_log_traceback` is the flag `__del__` reads: asyncio sets it when the
    exception is set and clears it when anything retrieves it, so it answers
    "was this claimed" directly and deterministically. Private, and named here
    because a private attribute in a test is a debt the next reader should see.
    """
    drifted = dict(
        DESCRIPTION,
        methods=[{"name": "do", "signature_hash": "sha256:MOVED", "params": [], "returns": None}],
    )
    client = _client(Recorder(description=drifted))
    client._verified = True

    calls = [asyncio.create_task(client._call("do", {}, int)) for _ in range(4)]
    await asyncio.sleep(0.015)
    task = client._reverify
    assert task is not None and not task.done(), "premise: a re-verify is in flight"

    for call in calls:
        call.cancel()
    await asyncio.gather(*calls, return_exceptions=True)
    await asyncio.sleep(0.05)

    assert task.done() and not task.cancelled(), (task.done(), task.cancelled())
    assert isinstance(task.exception(), ClientOutOfDateError), task.exception()
    # `task.exception()` above retrieves it, so read the flag FIRST next time --
    # this ordering is why the assertion below is captured before that call.


async def test_the_cohorts_exception_is_claimed_before_anyone_asks() -> None:
    """The assertion that actually fences the callback, read before retrieval.

    Reading `task.exception()` retrieves it, so a test that inspects the
    exception first cannot then tell whether anything else had claimed it. This
    one reads the flag and nothing else.
    """
    drifted = dict(
        DESCRIPTION,
        methods=[{"name": "do", "signature_hash": "sha256:MOVED", "params": [], "returns": None}],
    )
    client = _client(Recorder(description=drifted))
    client._verified = True

    calls = [asyncio.create_task(client._call("do", {}, int)) for _ in range(4)]
    await asyncio.sleep(0.015)
    task = client._reverify
    assert task is not None

    for call in calls:
        call.cancel()
    await asyncio.gather(*calls, return_exceptions=True)
    await asyncio.sleep(0.05)

    assert task.done() and not task.cancelled()
    assert task._log_traceback is False, (
        "the re-verify's exception was never claimed, so `Task.__del__` will log "
        "'Task exception was never retrieved' at ERROR during a rolling deploy"
    )


async def test_CONTROL_an_unclaimed_task_shows_the_flag_set() -> None:
    """Otherwise the assertion above passes on a flag that is never True."""

    async def boom() -> None:
        raise ValueError("deliberate")

    task = asyncio.create_task(boom())
    await asyncio.sleep(0.02)

    assert task.done() and not task.cancelled()
    assert task._log_traceback is True, "the harness cannot see an unclaimed exception"
    task.exception()  # claim it, so this test does not create the noise it describes

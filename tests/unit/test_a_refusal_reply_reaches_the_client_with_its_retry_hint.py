"""`RpcRefusedError` carries the `retry_after` and `details` the refusal reply carries.

A refusal that is a `RetryMessage` (the rate limiter's is one) adds `retry_after` and `details` to its
reply, and the service goes to some trouble to compute them. `raise_for_error_envelope`, the one place
the reply is read for `ServiceClient` and for `call_rpc`, turned the reply into a bare
`RpcRefusedError(reason)`: the caller could not read how long to wait, and `.details` stayed empty.
"""

import json
import pickle
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.exceptions import RpcRefusedError, raise_for_error_envelope
from cliffracer.core.extension import Extension, RetryMessage
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit


def _reply(**fields) -> dict:
    return {"success": False, "error": "refused: capacity", "code": "refused", **fields}


def _refusal(reply: dict) -> RpcRefusedError:
    with pytest.raises(RpcRefusedError) as caught:
        raise_for_error_envelope(reply, "svc.rpc.go")
    return caught.value


class Busy(Extension):
    async def worker_setup(self, ctx):
        refusal = RetryMessage("capacity", retry_after=7.5)
        refusal.details = {"queue_depth": 120}
        raise refusal


class Svc(CliffracerService):
    busy = Busy()

    @rpc
    async def go(self) -> str:
        return "ok"


async def test_a_retry_message_refusal_reaches_the_client_with_both_fields():
    async with ServiceTestHarness(Svc, config=ServiceConfig(name="svc", health_port=0)) as harness:
        reply = (await harness.rpc("go")).data

    refusal = _refusal(reply)

    assert refusal.reason == "capacity"
    assert refusal.retry_after == 7.5
    assert refusal.details == {"queue_depth": 120}


def _a_service_refusing_with(retry_after):
    class Refuses(Extension):
        async def worker_setup(self, ctx):
            raise RetryMessage("capacity", retry_after=retry_after)

    class Refused(CliffracerService):
        refuses = Refuses()

        @rpc
        async def go(self) -> str:
            return "ok"

    return Refused


@pytest.mark.parametrize(
    ("retry_after", "arrives"),
    [(7.5, 7.5), (0, 0), (0.5, 0.5), (None, None), (-0.5, None)],
    ids=["7.5", "zero", "half", "none", "below-zero"],
)
async def test_a_retry_after_of_zero_or_more_reaches_the_client_and_none_is_left_out(
    retry_after, arrives
):
    service = _a_service_refusing_with(retry_after)
    async with ServiceTestHarness(
        service, config=ServiceConfig(name="svc", health_port=0)
    ) as harness:
        reply = (await harness.rpc("go")).data

    refusal = _refusal(reply)
    assert refusal.reason == "capacity"
    assert refusal.retry_after == arrives
    if arrives is None:
        assert "retry_after" not in reply and "details" not in reply


async def test_call_rpc_raises_the_same_refusal_with_the_fields():
    svc = CliffracerService(ServiceConfig(name="caller", health_port=0))
    svc.nc = AsyncMock()
    svc.nc.request.return_value = SimpleNamespace(
        data=json.dumps(_reply(retry_after=3, details={"calls": 5})).encode(),
        headers={"Content-Type": "application/json"},
    )

    with pytest.raises(RpcRefusedError) as caught:
        await svc.call_rpc("other", "go")

    assert (caught.value.retry_after, caught.value.details) == (3.0, {"calls": 5})


def test_a_refusal_with_neither_field_is_what_it_always_was():
    refusal = _refusal(_reply())

    assert refusal.reason == "capacity"
    assert refusal.details == {}
    assert str(refusal) == "refused: capacity"


@pytest.mark.parametrize(
    "value", ["soon", True, False, -1, None, [3], {"s": 3}, float("inf"), float("nan")]
)
def test_a_retry_after_that_is_not_a_usable_number_is_none(value):
    assert _refusal(_reply(retry_after=value)).retry_after is None


@pytest.mark.parametrize("details", ["text", ["a"], 7, {}, None])
def test_details_that_are_not_a_non_empty_object_are_empty(details):
    assert _refusal(_reply(details=details)).details == {}


def test_CONTROL_zero_is_a_usable_retry_after():
    assert _refusal(_reply(retry_after=0)).retry_after == 0.0


def test_CONTROL_a_reply_from_a_service_that_predates_code_is_still_a_refusal_by_prefix():
    old = {"success": False, "error": "refused: capacity", "retry_after": 5, "details": {"a": 1}}

    refusal = _refusal(old)

    assert refusal.reason == "capacity"
    assert refusal.details == {}, "the fields are read from a reply that carries a code"


def test_a_refusal_survives_pickle_with_its_fields():
    again = pickle.loads(pickle.dumps(_refusal(_reply(retry_after=2.5, details={"k": "v"}))))

    assert (again.reason, again.retry_after, again.details) == ("capacity", 2.5, {"k": "v"})

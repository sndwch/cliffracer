"""An RPC refusal says how long to wait and what limit was hit, and keeps its three old fields.

The refusal reply is `{success, error, code, timestamp, correlation_id}`. A refusal that knows
more than its reason (a rate limit: the limit, the window, a fingerprint of the key, how long
until a permit is free) used to throw that away: the exception carried `details` and
`retry_after`, the durable path used `retry_after`, and an RPC caller saw only "refused", with no
way to back off. The reply now ADDS `retry_after` (seconds, when the refusal carries one) and
`details` (a non-empty dict, when it carries one). The three fields a caller parses today are
untouched, and a refusal with neither is byte-for-byte the reply it was.
"""

import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension, RejectMessage, RetryMessage

pytestmark = pytest.mark.unit

OLD_FIELDS = {"success", "error", "code", "timestamp", "correlation_id"}


class _DetailedRefusal(RetryMessage):
    def __init__(self, reason: str, details: dict, retry_after: float | None = None) -> None:
        super().__init__(reason, retry_after=retry_after)
        self.details = details


async def _reply_to_a_call_refused_by(raise_this: BaseException) -> dict:
    class Refuses(Extension):
        async def worker_setup(self, ctx) -> None:
            raise raise_this

    class Svc(CliffracerService):
        refuses = Refuses()

        @rpc
        async def work(self) -> int:
            return 1

    svc = Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject = "svc.rpc.work"
    msg.data = json.dumps({}).encode()
    msg.headers = {}
    await svc.container._handle_rpc_request(msg)
    (call,) = msg.respond.await_args_list
    return json.loads(call.args[0].decode())


async def test_CONTROL_a_plain_refusal_is_exactly_the_reply_it_was():
    reply = await _reply_to_a_call_refused_by(RejectMessage("not allowed"))

    assert set(reply) == OLD_FIELDS, reply
    assert (reply["success"], reply["error"], reply["code"]) == (
        False,
        "refused: not allowed",
        "refused",
    )


async def test_a_retry_hint_rides_on_the_refusal_without_changing_the_old_fields():
    reply = await _reply_to_a_call_refused_by(RetryMessage("slow down", retry_after=2.5))

    assert reply["retry_after"] == 2.5
    assert set(reply) == OLD_FIELDS | {"retry_after"}, reply
    assert (reply["success"], reply["error"], reply["code"]) == (
        False,
        "refused: slow down",
        "refused",
    )


async def test_details_ride_on_the_refusal():
    details = {"key": "sha256:0123456789ab", "calls": 10, "window": 60.0}

    reply = await _reply_to_a_call_refused_by(_DetailedRefusal("rate limit exceeded", details, 1.5))

    assert reply["details"] == details
    assert reply["retry_after"] == 1.5
    assert (reply["error"], reply["code"]) == ("refused: rate limit exceeded", "refused")


async def test_a_refusal_with_no_hint_and_empty_details_adds_nothing():
    reply = await _reply_to_a_call_refused_by(_DetailedRefusal("no hint", {}, None))

    assert set(reply) == OLD_FIELDS, reply


async def test_a_hook_that_crashed_is_the_service_being_broken_and_carries_no_hint():
    class Crashes(Exception):
        pass

    class Boom(Extension):
        fails_closed = True

        async def worker_setup(self, ctx) -> None:
            raise Crashes("the hook crashed")

    class Svc(CliffracerService):
        boom = Boom()

        @rpc
        async def work(self) -> int:
            return 1

    svc = Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject = "svc.rpc.work"
    msg.data = json.dumps({}).encode()
    msg.headers = {}
    await svc.container._handle_rpc_request(msg)
    reply = json.loads(msg.respond.await_args_list[0].args[0].decode())

    assert reply["code"] == "internal"
    assert "retry_after" not in reply and "details" not in reply, reply


async def test_details_that_cannot_be_serialised_do_not_cost_the_caller_its_refusal():
    """The reply must arrive: a bad `details` is stringified, never the reason there is no reply."""
    details = {"limit": 10, "reason": object(), "tags": {"a", "b"}}

    reply = await _reply_to_a_call_refused_by(_DetailedRefusal("rate limit exceeded", details, 1.5))

    assert (reply["success"], reply["error"], reply["code"]) == (
        False,
        "refused: rate limit exceeded",
        "refused",
    )
    assert reply["details"]["limit"] == 10
    assert isinstance(reply["details"]["reason"], str), reply
    assert reply["retry_after"] == 1.5


async def test_details_that_cannot_be_written_at_all_are_dropped_and_the_refusal_still_arrives():
    cyclic: dict = {}
    cyclic["self"] = cyclic
    lines: list[str] = []
    from loguru import logger

    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        reply = await _reply_to_a_call_refused_by(_DetailedRefusal("no way", cyclic, 2.0))
    finally:
        logger.remove(sink)

    assert "details" not in reply, reply
    assert (reply["success"], reply["error"], reply["code"]) == (
        False,
        "refused: no way",
        "refused",
    )
    assert reply["retry_after"] == 2.0
    assert any("details" in line for line in lines), lines


@pytest.mark.parametrize("not_a_number", ["soon", True, None, [1]])
async def test_a_retry_after_that_is_not_a_number_is_not_put_on_the_wire(not_a_number):
    refusal = _DetailedRefusal("slow down", {}, None)
    refusal.retry_after = not_a_number

    reply = await _reply_to_a_call_refused_by(refusal)

    assert "retry_after" not in reply, reply


@pytest.mark.parametrize("not_a_dict", [["a"], "text", 5, {}])
async def test_details_that_are_not_a_non_empty_dict_are_not_put_on_the_wire(not_a_dict):
    refusal = _DetailedRefusal("refused", {})
    refusal.details = not_a_dict

    reply = await _reply_to_a_call_refused_by(refusal)

    assert "details" not in reply, reply

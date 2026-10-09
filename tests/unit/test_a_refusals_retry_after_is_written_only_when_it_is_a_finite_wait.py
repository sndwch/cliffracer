"""A refusal's `retry_after` is written into the RPC reply only when it is a finite wait.

`_what_a_refusal_adds` wrote any number the refusal carried, so a `RetryMessage` with a
`retry_after` of `nan`, `inf` or a negative number put `NaN`, `Infinity` or a negative delay into
the reply. `NaN` and `Infinity` are not JSON, so a client in another language could not read the
reply at all. The NAK path already refused those values as a delay; both paths now ask the same
question of the number, `is_a_finite_delay`.

Zero is still written: it is a number a caller can wait, and the NAK path's treatment of it as no
hint is about which delay to use for redelivery, not about what a refusal may say.
"""

import json
import math
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.dispatch.jetstream import _is_a_usable_delay
from cliffracer.core.extension import RetryMessage, is_a_finite_delay
from cliffracer.testing import refuse_a_reply_with_no_subject

pytestmark = pytest.mark.unit


class Msg:
    def __init__(self, subject: str) -> None:
        self.subject = subject
        self.data = b"{}"
        self.headers: dict[str, str] = {}
        self.reply = "_INBOX.reply"
        self.out: bytes | None = None

    async def respond(self, data: bytes) -> None:
        refuse_a_reply_with_no_subject(self)
        self.out = data


def _refused_with(retry_after: Any) -> Msg:
    class Service(CliffracerService):
        @rpc
        async def work(self) -> int:
            raise RetryMessage("busy", retry_after=retry_after)

    return _answer(Service)


def _answer(service_class: type[CliffracerService]) -> Msg:
    import asyncio

    service = service_class(ServiceConfig(name="svc"))
    service._discover_handlers()
    msg = Msg("svc.work")
    asyncio.run(service.container._handle_rpc_request(msg))
    return msg


def _refuse_a_constant(name: str) -> Any:
    raise AssertionError(f"the reply holds {name}, which is not JSON")


def _reply(retry_after: Any) -> dict[str, Any]:
    msg = _refused_with(retry_after)
    assert msg.out is not None
    return json.loads(msg.out.decode(), parse_constant=_refuse_a_constant)


@pytest.mark.parametrize(
    "retry_after",
    [
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="inf"),
        pytest.param(-math.inf, id="minus-inf"),
        pytest.param(-3.0, id="negative-float"),
        pytest.param(-1, id="negative-int"),
        pytest.param(True, id="a-bool"),
        pytest.param(None, id="none"),
        pytest.param("5", id="a-string"),
    ],
)
def test_a_retry_after_that_is_not_a_finite_wait_is_not_written(retry_after):
    reply = _reply(retry_after)

    assert reply["success"] is False and reply["code"] == "refused"
    assert "retry_after" not in reply


@pytest.mark.parametrize(
    ("retry_after", "written"),
    [
        pytest.param(1.5, 1.5, id="a-float"),
        pytest.param(7, 7, id="an-int"),
        pytest.param(0, 0, id="zero"),
        pytest.param(0.0, 0.0, id="zero-float"),
        pytest.param(1e9, 1e9, id="a-very-long-wait"),
    ],
)
def test_CONTROL_a_finite_wait_of_zero_or_more_is_written_as_it_is(retry_after, written):
    reply = _reply(retry_after)

    assert reply["retry_after"] == written


def test_a_refusal_without_a_retry_after_gets_the_reply_it_always_got():
    class Plain(CliffracerService):
        @rpc
        async def work(self) -> int:
            from cliffracer.core.extension import RejectMessage

            raise RejectMessage("no")

    msg = _answer(Plain)
    assert msg.out is not None
    reply = json.loads(msg.out.decode())

    assert set(reply) == {"success", "error", "code", "timestamp", "correlation_id"}


@pytest.mark.parametrize(
    "value",
    [1.5, 7, 0, 0.0, -3.0, -1, math.nan, math.inf, -math.inf, True, False, None, "5", [1], b"1"],
    ids=repr,
)
def test_the_nak_delay_and_the_reply_agree_about_which_values_are_numbers(value):
    finite = is_a_finite_delay(value)

    assert _is_a_usable_delay(value) == (finite and value > 0)  # type: ignore[operator]
    if finite and value >= 0:  # type: ignore[operator]
        assert _reply(value)["retry_after"] == value
    else:
        assert "retry_after" not in _reply(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.5, True),
        (7, True),
        (0, True),
        (-2.5, True),
        (math.nan, False),
        (math.inf, False),
        (-math.inf, False),
        (True, False),
        (False, False),
        (None, False),
        ("5", False),
    ],
)
def test_is_a_finite_delay(value, expected):
    assert is_a_finite_delay(value) is expected

"""The request's Content-Type decides how it is decoded and in what format it is answered.

Three things hold, and each is tested at the point that decides rather than at a rendering of it:

1. `reply_headers` carries a Content-Type and a correlation id only when there is one to carry --
   a missing type, an empty or non-string id, adds nothing -- and `answer` stamps those headers
   only on a message that has a headers attribute.
2. `handle_rpc_request` picks the reply format from the request: an explicit msgpack or JSON type
   decides; with no type at all the body is sniffed (a leading `{` or `[` means JSON); any OTHER
   declared type leaves the service's configured format alone. The header is found by its name
   in any case, wherever it sits among the headers, and the FIRST one wins.
3. `handle_async_request` reads the same header the same way. A DECLARED JSON type is honoured:
   a msgpack body behind it is not guessed at, which the same body with no type would be.

The format tests configure the service for msgpack, because with the default JSON a wrongly
chosen "json" is the answer anyway and cannot be told from the right one.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import msgpack
import pytest

from cliffracer import CliffracerService, ServiceConfig, async_rpc, rpc
from cliffracer.core.dispatch.rpc import answer, reply_headers
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit

JSON = "application/json"
MSGPACK = "application/msgpack"


# --- reply_headers and answer -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "correlation_id", "expected"),
    [
        (None, None, {}),
        ("", "", {}),
        (None, 123, {}),
        ("", "abc", {"X-Correlation-ID": "abc"}),
        (JSON, None, {"Content-Type": JSON}),
        (JSON, "", {"Content-Type": JSON}),
        (JSON, 123, {"Content-Type": JSON}),
        (MSGPACK, "abc", {"Content-Type": MSGPACK, "X-Correlation-ID": "abc"}),
    ],
    ids=[
        "nothing",
        "empty_both",
        "non_string_id",
        "id_only",
        "type_only_none_id",
        "type_only_empty_id",
        "type_only_int_id",
        "both",
    ],
)
def test_a_reply_carries_only_the_headers_it_has_a_value_for(
    content_type, correlation_id, expected
):
    assert reply_headers(content_type, correlation_id) == expected


async def test_a_reply_to_a_message_without_a_headers_attribute_is_sent_and_stamps_none():
    """`answer` does not grow a headers attribute on an object that has none.

    The message allows only `respond`, so assigning `headers` raises: a stamp attempted on it
    would fail the reply, and the caller would get nothing.
    """
    msg = MagicMock(spec_set=["respond"])
    msg.respond = AsyncMock()

    await answer(msg, b"payload", content_type=JSON, correlation_id="abc")

    msg.respond.assert_awaited_once_with(b"payload")
    assert not hasattr(msg, "headers")


async def test_CONTROL_a_reply_to_a_message_with_headers_is_stamped_with_them():
    msg = MockMessage("s.rpc.echo", b"{}", headers={"X-Caller": "kept?"})

    await answer(msg, b"payload", content_type=JSON, correlation_id="abc")

    assert msg.response_headers == {"Content-Type": JSON, "X-Correlation-ID": "abc"}


# --- the RPC arm: which format answers ----------------------------------------------------------


class Svc(CliffracerService):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.seen: list[int] = []

    @rpc
    async def echo(self, value: int) -> int:
        return value

    @async_rpc
    async def note(self, value: int) -> None:
        self.seen.append(value)


async def _service(fmt: str) -> Svc:
    svc = Svc(ServiceConfig(name="s", health_listener=False, serialization_format=fmt))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    return svc


async def _ask(fmt: str, headers: dict[str, str], body: bytes) -> MockMessage:
    svc = await _service(fmt)
    msg = MockMessage("s.rpc.echo", body, headers=headers)
    await svc.container.dispatcher.handle_rpc_request(msg)
    return msg


def _reply_type(msg: MockMessage) -> str | None:
    return msg.response_headers.get("Content-Type")


async def test_with_no_type_a_msgpack_body_is_answered_in_the_services_format():
    msg = await _ask("msgpack", {"X-Other": "1"}, msgpack.packb({"value": 7}))

    assert _reply_type(msg) == MSGPACK
    assert msgpack.unpackb(msg.responded_data)["result"] == 7


async def test_with_no_type_an_empty_body_is_answered_in_the_services_format():
    msg = await _ask("msgpack", {"X-Other": "1"}, b"")

    assert _reply_type(msg) == MSGPACK


async def test_with_no_type_a_json_body_is_answered_in_json():
    """CONTROL for the two above: the sniff does fire, for a body that starts like JSON."""
    msg = await _ask("msgpack", {"X-Other": "1"}, b'  {"value": 7}')

    assert _reply_type(msg) == JSON
    assert json.loads(msg.responded_data)["result"] == 7


async def test_with_no_type_a_json_array_is_answered_in_json():
    msg = await _ask("msgpack", {"X-Other": "1"}, b"[1]")

    assert _reply_type(msg) == JSON


async def test_a_type_that_is_neither_json_nor_msgpack_leaves_the_services_format_alone():
    """The body is JSON-shaped, and the declared type is not JSON: the sniff must not run."""
    msg = await _ask("msgpack", {"Content-Type": "text/plain"}, b'{"value": 7}')

    assert _reply_type(msg) == MSGPACK
    assert msgpack.unpackb(msg.responded_data)["result"] == 7


async def test_a_declared_json_type_is_answered_in_json_whatever_the_service_is_set_to():
    msg = await _ask("msgpack", {"Content-Type": JSON}, b'{"value": 7}')

    assert _reply_type(msg) == JSON


async def test_a_declared_msgpack_type_is_answered_in_msgpack_whatever_the_service_is_set_to():
    msg = await _ask("json", {"Content-Type": MSGPACK}, msgpack.packb({"value": 7}))

    assert _reply_type(msg) == MSGPACK


# --- the RPC arm: where the header is found -----------------------------------------------------


async def test_the_content_type_is_found_behind_a_header_that_is_not_one():
    msg = await _ask(
        "json", {"X-Trace": "a;b", "content-type": MSGPACK}, msgpack.packb({"value": 7})
    )

    assert msgpack.unpackb(msg.responded_data)["result"] == 7, msg.responded_data
    assert _reply_type(msg) == MSGPACK


async def test_the_first_of_two_content_type_headers_decides():
    msg = await _ask(
        "json",
        {"Content-Type": JSON, "content-type": MSGPACK},
        b'{"value": 7}',
    )

    assert json.loads(msg.responded_data)["result"] == 7, msg.responded_data
    assert _reply_type(msg) == JSON


# --- the async arm ------------------------------------------------------------------------------


async def _note(headers: dict[str, str], body: bytes) -> list[int]:
    svc = await _service("json")
    msg = MockMessage("s.async.note", body, headers=headers, reply="")
    await svc.container.dispatcher.handle_async_request(msg)
    return svc.seen


async def test_async_the_content_type_is_found_behind_a_header_that_is_not_one():
    assert await _note({"X-Trace": "a", "Content-Type": MSGPACK}, msgpack.packb({"value": 7})) == [
        7
    ]


async def test_async_a_declared_json_type_is_not_second_guessed_for_a_msgpack_body():
    assert await _note({"Content-Type": JSON}, msgpack.packb({"value": 7})) == []


async def test_CONTROL_async_the_same_msgpack_body_with_no_type_is_read_by_fallback():
    """Without this the test above would pass for a handler that was never reachable."""
    assert await _note({"X-Trace": "a"}, msgpack.packb({"value": 7})) == [7]


async def test_async_the_first_of_two_content_type_headers_decides():
    assert await _note({"Content-Type": JSON, "content-type": MSGPACK}, b'{"value": 7}') == [7]

"""An inbound correlation id is printable text of bounded length, or it is not used.

The id comes from a header any publisher on the broker can set, or from a payload field. It is logged
on every line of its request and copied onto every message the handler sends. The only check was for
CR and LF, so an ANSI escape that rewrites earlier terminal lines, a vertical tab, form feed, NEL or
U+2028 that splits a logged line into several records, and an id of any length were all accepted,
logged intact and re-sent. An id that is not printable, or is longer than 256 characters, is treated
as absent, as one holding a CR or LF already was: the next source is tried, or a new id is made.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.correlation import CorrelationContext, correlation_id_var

pytestmark = pytest.mark.unit

# Written out, not read from the module: the limit is part of what the docs promise.
MAX_CORRELATION_ID_LENGTH = 256

REFUSED = [
    pytest.param("abc\x1b[2K\x1b[1Adef", id="ansi-erase-line-and-cursor-up"),
    pytest.param("abc\x1bdef", id="escape"),
    pytest.param("abc\x0bdef", id="vertical-tab"),
    pytest.param("abc\x0cdef", id="form-feed"),
    pytest.param("abc\x85def", id="nel"),
    pytest.param("abc def", id="line-separator"),
    pytest.param("abc def", id="paragraph-separator"),
    pytest.param("abc\x00def", id="nul"),
    pytest.param("abc\tdef", id="tab"),
    pytest.param("abc\x7fdef", id="delete"),
    pytest.param("abc\ndef", id="lf"),
    pytest.param("abc\rdef", id="cr"),
    pytest.param("a" * (MAX_CORRELATION_ID_LENGTH + 1), id="one-over-the-limit"),
    pytest.param("a" * 600_000, id="600-kb"),
]

ACCEPTED = [
    pytest.param("123e4567-e89b-12d3-a456-426614174000", id="uuid"),
    pytest.param("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", id="traceparent"),
    pytest.param("corr_0123456789abcdef", id="generated-shape"),
    pytest.param("a" * MAX_CORRELATION_ID_LENGTH, id="exactly-the-limit"),
    pytest.param("订单-42", id="unicode-letters"),
    pytest.param("order 42 retry", id="spaces"),
    pytest.param("req/42:7;x=y", id="punctuation"),
]


@pytest.mark.parametrize("value", REFUSED)
def test_a_value_that_is_not_printable_or_is_too_long_is_refused(value):
    from cliffracer.core.correlation import refusal_of

    assert refusal_of(value) is not None


@pytest.mark.parametrize("value", ACCEPTED)
def test_CONTROL_a_value_that_is_printable_and_short_is_accepted(value):
    from cliffracer.core.correlation import refusal_of

    assert refusal_of(value) is None
    assert CorrelationContext.new_id_unless_given(value) == value
    assert CorrelationContext.get_or_create_id(value) == value
    assert CorrelationContext.for_message({"X-Correlation-ID": value}, {}) == value
    assert CorrelationContext.for_message({}, {"correlation_id": value}) == value


@pytest.mark.parametrize("value", REFUSED)
def test_every_resolver_treats_it_as_absent_and_makes_a_new_id(value):
    for resolved in (
        CorrelationContext.new_id_unless_given(value),
        CorrelationContext.get_or_create_id(value),
        CorrelationContext.for_message({"X-Correlation-ID": value}, {}),
        CorrelationContext.for_message({}, {"correlation_id": value}),
        CorrelationContext.for_message({"request-id": value, "trace-id": value}, {}),
    ):
        assert resolved != value and resolved.startswith("corr_")


@pytest.mark.parametrize("value", REFUSED)
def test_a_refused_header_falls_through_to_the_next_source(value):
    assert (
        CorrelationContext.for_message({"X-Correlation-ID": value, "X-Request-ID": "next"}, {})
        == "next"
    )
    assert (
        CorrelationContext.for_message({"X-Correlation-ID": value}, {"correlation_id": "payload"})
        == "payload"
    )


@pytest.mark.parametrize("value", REFUSED)
def test_an_ambient_id_that_is_refused_is_not_reused(value):
    token = correlation_id_var.set(value)
    try:
        resolved = CorrelationContext.get_or_create_id()
    finally:
        correlation_id_var.reset(token)

    assert resolved != value and resolved.startswith("corr_")


@pytest.mark.parametrize("value", REFUSED)
def test_the_warning_for_a_refused_id_is_one_short_escaped_line(value):
    lines: list[str] = []
    sink = logger.add(
        lambda message: lines.append(str(message)), level="WARNING", format="{message}"
    )
    try:
        CorrelationContext.for_message({"X-Correlation-ID": value}, {})
    finally:
        logger.remove(sink)

    (line,) = lines
    assert "Rejected invalid correlation ID" in line
    assert len(line) < 400, "the refused id was logged at its own length"
    assert line.count("\n") == 1 and line.endswith("\n"), "the warning is more than one line"
    assert len(line.splitlines()) == 1
    for character in "\x1b\x0b\x0c\x85  \x00\t\x7f\r":
        assert character not in line


class Svc(CliffracerService):
    seen: dict = {}

    @listener("things.happened", fanout=True)
    async def on_thing(self, subject: str) -> None:
        Svc.seen["id"] = CorrelationContext.get()
        Svc.seen["outgoing"] = CorrelationContext.inject_into_headers({})


async def _deliver(header_value: str, payload: dict | None = None) -> dict:
    svc = Svc(ServiceConfig(name="bounded", health_port=0))
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    msg = AsyncMock()
    msg.subject = "things.happened"
    msg.data = json.dumps(payload or {}).encode()
    msg.headers = {"Content-Type": "application/json", "X-Correlation-ID": header_value}
    Svc.seen = {}

    await asyncio.wait_for(svc.container.event_dispatcher.handle_event(msg), timeout=5)

    return Svc.seen


@pytest.mark.parametrize("value", REFUSED)
async def test_a_delivered_event_runs_under_a_new_id_and_sends_that_one_on(value):
    seen = await _deliver(value)

    assert seen["id"] != value and seen["id"].startswith("corr_")
    assert seen["outgoing"] == {"X-Correlation-ID": seen["id"]}


@pytest.mark.parametrize("value", ACCEPTED)
async def test_CONTROL_a_delivered_event_keeps_an_accepted_id_and_sends_it_on(value):
    seen = await _deliver(value)

    assert seen["id"] == value
    assert seen["outgoing"] == {"X-Correlation-ID": value}

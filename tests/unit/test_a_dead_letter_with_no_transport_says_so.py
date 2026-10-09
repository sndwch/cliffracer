"""A dead letter that cannot be published because there is no connection names that, not `()`.

`DeadLetterPublisher.publish_dlq` guarded its transport with bare `assert`s. A bare AssertionError
stringifies to `""`, so the three handlers that swallow a failed dead-letter publish logged
"Failed to dead-letter ... ()" with no reason; under `python -O` the asserts vanish and the same
state surfaced as an AttributeError on None. The condition is now an explicit builtin ConnectionError that
says what is missing, and the swallow log lines carry the exception's type as well as its text.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from cliffracer.core.dispatch import DeadLetterPublisher
from cliffracer.core.jetstream import StreamSpec
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit


class _Strict(BaseModel):
    number: int


class _Recorder:
    """Stands in for the publisher's logger: keeps every line, by level."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def __getattr__(self, level: str):
        return lambda text, *a, **k: self.lines.append((level, text))

    def errors(self) -> list[str]:
        return [t for level, t in self.lines if level == "error"]


def _publisher(
    *, nc=None, js=None, jetstream_active=False
) -> tuple[DeadLetterPublisher, _Recorder]:
    cfg = ServiceConfig(name="orders", dlq_subject="dlq.{service}")
    conn = SimpleNamespace(nc=nc, js=js, jetstream_active=jetstream_active)
    log = _Recorder()
    return DeadLetterPublisher(cfg, lambda: conn, logger=log), log


def _validation_error() -> object:
    try:
        _Strict.model_validate({"number": "not a number"})
    except Exception as exc:  # pydantic.ValidationError
        return exc
    raise AssertionError("the payload was meant to be invalid")


async def test_publish_with_no_connection_raises_an_error_that_names_the_condition():
    dlq, _ = _publisher(nc=None)

    with pytest.raises(ConnectionError, match="no NATS connection"):
        await dlq.publish_dlq("dlq.orders", {"x": 1})


async def test_publish_with_jetstream_active_and_no_context_names_that_condition():
    cfg = ServiceConfig(
        name="orders",
        dlq_subject="dlq.{service}",
        jetstream_streams=[StreamSpec(name="dead_letters", subjects=["dlq.>"])],
    )
    conn = SimpleNamespace(nc=MagicMock(), js=None, jetstream_active=True)
    dlq = DeadLetterPublisher(cfg, lambda: conn, logger=_Recorder())

    with pytest.raises(ConnectionError, match="no JetStream context"):
        await dlq.publish_dlq("dlq.orders", {"x": 1})


async def test_the_swallowed_failure_in_each_handler_names_its_cause():
    for handler in ("invalid", "decode", "terminated"):
        dlq, log = _publisher(nc=None)
        msg = SimpleNamespace(subject="orders.events.x", data=b"{}", headers={}, metadata=None)

        if handler == "invalid":
            await dlq.handle_invalid_message(
                "orders.strict", {"number": "x"}, _validation_error(), _Strict, "deadletter"
            )
        elif handler == "decode":
            await dlq.dead_letter_decode_error(msg, ValueError("bad"))
        else:
            await dlq.dead_letter_terminated(msg, "boom", 3)

        (line,) = log.errors()
        assert "()" not in line, f"{handler}: a reason-less failure line: {line}"
        assert "ConnectionError" in line and "no NATS connection" in line, f"{handler}: {line}"


async def test_control_with_a_connection_the_dead_letter_is_published_and_nothing_is_logged_as_failed():
    nc = MagicMock()
    nc.publish = AsyncMock()
    dlq, log = _publisher(nc=nc)

    await dlq.handle_invalid_message(
        "orders.strict", {"number": "x"}, _validation_error(), _Strict, "deadletter"
    )

    nc.publish.assert_awaited_once()
    assert nc.publish.await_args.args[0] == "dlq.orders"
    assert log.errors() == []

"""Tests for the MockMessage envelope's acknowledgement and reply rules."""

import pytest
from nats.errors import Error, MsgAlreadyAckdError, NotJSMessageError

from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit

TERMINAL_ACKS = ("ack", "nak", "term")


@pytest.mark.parametrize("second", TERMINAL_ACKS)
@pytest.mark.parametrize("first", TERMINAL_ACKS)
async def test_a_second_terminal_acknowledgement_raises(first: str, second: str) -> None:
    """A terminal acknowledgement on an already-acknowledged message raises."""
    msg = MockMessage(subject="orders.created")
    await getattr(msg, first)()

    with pytest.raises(MsgAlreadyAckdError):
        await getattr(msg, second)()


@pytest.mark.parametrize("reply", [None, ""])
@pytest.mark.parametrize("method", (*TERMINAL_ACKS, "in_progress"))
async def test_acknowledging_without_a_reply_subject_raises(method: str, reply: str | None) -> None:
    """A message carrying no reply subject is not a JetStream message."""
    msg = MockMessage(subject="orders.created", reply=reply)

    with pytest.raises(NotJSMessageError):
        await getattr(msg, method)()


async def test_in_progress_repeats_and_leaves_the_message_unacknowledged() -> None:
    """An in-progress pulse is not terminal, so it repeats and a later ack still lands."""
    msg = MockMessage(subject="orders.created")

    await msg.in_progress()
    await msg.in_progress()

    assert msg.is_acked is False
    await msg.ack()
    assert msg.is_acked is True


# --- replying ----------------------------------------------------------------


@pytest.mark.parametrize("reply", [None, ""])
async def test_responding_without_a_reply_subject_raises(reply: str | None) -> None:
    """`nats.aio.msg.Msg.respond` raises when there is nowhere to reply to.

    Recording it instead made a handler that replies to a fire-and-forget
    message read as a successful RPC here and fail in production.
    """
    msg = MockMessage(subject="orders.created", reply=reply)

    with pytest.raises(Error) as raised:
        await msg.respond(b'{"ok": true}')

    assert str(raised.value) == "no reply subject available", str(raised.value)
    assert msg.responded_data is None


@pytest.mark.parametrize("reply", [None, ""])
async def test_the_refusal_is_the_generic_error_and_not_the_jetstream_one(
    reply: str | None,
) -> None:
    """The type is part of the contract, and `pytest.raises` cannot check it alone.

    `NotJSMessageError` is a SUBCLASS of `Error`, so the test above would pass
    just as well if `respond` were switched to `_check_reply` -- which raises
    the JetStream error and would also refuse an already-acknowledged message.
    The real `Msg.respond` raises the generic `Error`, so the exact type is
    asserted here.
    """
    msg = MockMessage(subject="orders.created", reply=reply)

    with pytest.raises(Error) as raised:
        await msg.respond(b"{}")

    assert type(raised.value) is Error, type(raised.value).__name__
    assert not isinstance(raised.value, NotJSMessageError)


async def test_responding_is_still_permitted_after_an_acknowledgement() -> None:
    """The other half of that asymmetry: acknowledging does not close the reply.

    `_check_reply` refuses a second terminal acknowledgement, and reusing it for
    `respond` would refuse this -- which the real `Msg` permits, since the two
    have nothing to do with each other.
    """
    msg = MockMessage(subject="orders.created")
    await msg.ack()

    await msg.respond(b'{"ok": true}')

    assert msg.responded_data == b'{"ok": true}'


async def test_CONTROL_a_message_with_a_reply_subject_records_the_response() -> None:
    """So the rule is not "always refuse", which every test above would satisfy."""
    msg = MockMessage(subject="orders.created", data=b"{}", headers={"X-Trace": "abc"})

    await msg.respond(b'{"result": 1}')

    assert msg.responded_data == b'{"result": 1}'
    assert msg.response_headers == {"X-Trace": "abc"}


@pytest.mark.parametrize(("reply", "recorded"), [("_INBOX.test", True), (None, False)])
async def test_a_refused_attempt_is_still_counted(reply: str | None, recorded: bool) -> None:
    """`respond_calls` counts attempts, so "never called" is assertable.

    A caller cannot use `responded_data` for that: every `msg.respond` in the
    dispatcher sits inside `except Exception` with a debug log, so a refused
    attempt leaves it at None and looks exactly like no attempt. Two tests in
    `tests/unit/test_async_rpc.py` depend on this counter for precisely that
    reason.
    """
    msg = MockMessage(subject="orders.created", reply=reply)
    assert msg.respond_calls == 0

    try:
        await msg.respond(b"{}")
    except Error:
        pass

    assert msg.respond_calls == 1
    assert (msg.responded_data is not None) is recorded


def test_the_rule_refuses_a_double_that_has_no_reply_attribute_at_all():
    """`refuse_a_reply_with_no_subject` is exported for other people's doubles.

    The real `Msg` always has a `reply`, so its own check is `if not
    self.reply`. A hand-written double may simply not define the attribute --
    five in this repository did not -- and for one of those, absent is the same
    answer as empty: it has no reply subject, so it cannot be replied to. This
    pins the `getattr` default, which no double in this tree exercises now that
    each carries a `reply`, and which a stricter read would silently drop.
    """
    from cliffracer.testing import refuse_a_reply_with_no_subject

    class NoReplyAttribute:
        pass

    with pytest.raises(Error, match="no reply subject"):
        refuse_a_reply_with_no_subject(NoReplyAttribute())


def test_CONTROL_the_rule_permits_a_double_that_has_one():
    """So the test above is not satisfied by refusing everything."""
    from cliffracer.testing import refuse_a_reply_with_no_subject

    class WithReply:
        reply = "_INBOX.somewhere"

    refuse_a_reply_with_no_subject(WithReply())

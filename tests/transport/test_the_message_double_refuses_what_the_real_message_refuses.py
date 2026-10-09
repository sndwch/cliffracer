"""The transport tier's message double acknowledges as `nats.aio.msg.Msg` does.

`MockJetStreamMsg` set its acknowledged flag on `ack`, `nak` and `term` and never read it, and checked
no reply subject: a test could acknowledge a message twice, nak after a term, or acknowledge a
message with no reply at all, and the double accepted what the real client refuses
(`MsgAlreadyAckdError`, `NotJSMessageError`). `dispatch/events.py` explains that a second terminal
acknowledgement "a real client refuses and safe_term swallows into a warning": under the permissive
double that case could not be observed. A core message now refuses acknowledgement and its metadata; a
delivery from a stream carries an ack subject and real metadata and acknowledges once.
"""

import nats.aio.msg as natsmsg
import nats.errors
import pytest

from .conftest import MockJetStreamMsg

pytestmark = pytest.mark.unit


def _core() -> MockJetStreamMsg:
    return MockJetStreamMsg(subject="orders.created", data=b"{}")


def _delivery(**kwargs) -> MockJetStreamMsg:
    return MockJetStreamMsg(subject="orders.created", data=b"{}", from_jetstream=True, **kwargs)


@pytest.mark.parametrize("operation", ["ack", "nak", "term", "in_progress"])
async def test_a_core_message_refuses_every_acknowledgement(operation):
    msg = _core()

    with pytest.raises(nats.errors.NotJSMessageError):
        await getattr(msg, operation)()

    assert (msg.ack_calls, msg.nak_calls, msg.term_calls, msg.in_progress_calls) == (0, [], 0, 0)


def test_a_core_messages_metadata_raises_as_the_real_property_does():
    with pytest.raises(nats.errors.NotJSMessageError):
        _ = _core().metadata


@pytest.mark.parametrize("first", ["ack", "nak", "term"])
@pytest.mark.parametrize("second", ["ack", "nak", "term"])
async def test_a_delivery_takes_one_terminal_acknowledgement(first, second):
    msg = _delivery()
    await getattr(msg, first)()

    with pytest.raises(nats.errors.MsgAlreadyAckdError):
        await getattr(msg, second)()

    counted = (msg.ack_calls, len(msg.nak_calls), msg.term_calls)
    assert sum(counted) == 1, "the refused acknowledgement was not counted"


async def test_a_delivery_can_report_progress_any_number_of_times_and_still_be_acknowledged():
    msg = _delivery()

    await msg.in_progress()
    await msg.in_progress()
    await msg.ack()

    assert (msg.in_progress_calls, msg.ack_calls) == (2, 1)


def test_a_delivery_carries_the_metadata_the_real_message_parses_from_its_ack_subject():
    msg = _delivery(num_delivered=3, stream="ORDERS", consumer="billing")
    real = natsmsg.Msg.Metadata._from_reply(msg.reply)

    assert msg.reply.startswith("$JS.ACK.ORDERS.billing.3.")
    assert msg.metadata == real
    assert (msg.metadata.num_delivered, msg.metadata.stream, msg.metadata.consumer) == (
        3,
        "ORDERS",
        "billing",
    )
    assert isinstance(msg.metadata.sequence, natsmsg.Msg.Metadata.SequencePair)


async def test_a_consumer_delivery_carries_its_consumer_and_a_core_message_does_not():
    """A delivery from a stream names its consumer and acknowledges once; a core message refuses.

    The broker does not model JetStream, so the delivery is built here as the carve-out tests that
    dispatch to the container build theirs.
    """
    delivery = _delivery(consumer="billing")
    plain = _core()

    assert delivery.reply and delivery.metadata.consumer == "billing"
    assert plain.reply is None
    with pytest.raises(nats.errors.NotJSMessageError):
        await plain.ack()
    await delivery.ack()
    with pytest.raises(nats.errors.MsgAlreadyAckdError):
        await delivery.ack()


def test_CONTROL_a_request_still_carries_the_reply_inbox_it_was_given():
    msg = MockJetStreamMsg(subject="svc.rpc.x", data=b"{}", reply="_INBOX.caller")

    assert msg.reply == "_INBOX.caller"

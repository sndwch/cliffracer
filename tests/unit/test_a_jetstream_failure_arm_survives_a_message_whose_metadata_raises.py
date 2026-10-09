"""A failed JetStream delivery is NAKed, whatever its `.metadata` does.

`nats.aio.msg.Msg.metadata` is a property that raises `NotJSMessageError` when the reply is not a
JetStream ack subject, and `getattr(msg, "metadata", None)` catches only `AttributeError`.
`dlq.message_metadata` exists for that, and the dead-letter publisher reads through it. Both failure
arms of `handle_jetstream_event` read the delivery count with the bare `getattr`, so for such a message
the arm that was to NAK the handler's failure raised instead, and the message was neither
acknowledged, refused nor terminated: it waited for `ack_wait` and the exception left the task.
"""

import json

import nats.aio.msg as natsmsg
import pytest
from nats.errors import NotJSMessageError

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.extension import Extension, RetryMessage
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import MockMessage, ServiceTestHarness

pytestmark = pytest.mark.unit

ACK_SUBJECT = "$JS.ACK.D.prober.{delivered}.42.7.1700000000000000000.0"


class NoMetadataMsg(MockMessage):
    """A message whose `.metadata` raises, as a real one does for a reply that is not an ack subject."""

    @property
    def metadata(self):
        raise NotJSMessageError

    @metadata.setter
    def metadata(self, value):  # `MockMessage.__init__` assigns it
        pass


class Failing(CliffracerService):
    @listener("probe.declared.thing", durable="prober")
    async def on_thing(self, subject: str, value: int = 0) -> None:
        raise RuntimeError("handler exploded")


class Busy(Extension):
    async def worker_setup(self, ctx):
        raise RetryMessage("busy", retry_after=2.0)


class Deferring(CliffracerService):
    busy = Busy()

    @listener("probe.declared.thing", durable="prober")
    async def on_thing(self, subject: str, value: int = 0) -> None:
        return None


def _config(max_deliver: int = 3) -> ServiceConfig:
    return ServiceConfig(
        name="probe_svc",
        health_port=0,
        jetstream_enabled=True,
        jetstream_max_deliver=max_deliver,
        jetstream_streams=[
            StreamSpec(name="D", subjects=["probe.declared.>"]),
            StreamSpec(name="DLQ", subjects=["dlq.>"]),
        ],
    )


def _msg(cls, delivered: int = 1, **extra):
    return cls(
        "probe.declared.thing",
        json.dumps({"value": 1}).encode(),
        {"Content-Type": "application/json"},
        ACK_SUBJECT.format(delivered=delivered),
        **extra,
    )


async def _deliver(service_cls, msg, max_deliver: int = 3) -> None:
    async with ServiceTestHarness(service_cls, config=_config(max_deliver)) as harness:
        await harness.container.dispatcher.jetstream.handle_jetstream_event(msg, pattern=None)


# A delivery whose count cannot be read is counted as the first: below a limit of 2 as of 3, and
# NAKed after the first delivery's backoff.
@pytest.mark.parametrize("max_deliver", [3, 2])
async def test_a_handler_failure_is_nacked_when_the_metadata_raises(max_deliver):
    msg = _msg(NoMetadataMsg)

    await _deliver(Failing, msg, max_deliver)

    assert msg.nacked and not msg.terminated and not msg.acked
    assert msg.nak_delay == _config(max_deliver).jetstream_nak_backoff


@pytest.mark.parametrize("max_deliver", [3, 2])
async def test_a_deferral_is_nacked_with_its_delay_when_the_metadata_raises(max_deliver):
    msg = _msg(NoMetadataMsg)

    await _deliver(Deferring, msg, max_deliver)

    assert msg.nacked and not msg.terminated and msg.nak_delay == 2.0


async def test_CONTROL_a_handler_failure_below_the_limit_is_nacked_with_real_metadata():
    real = natsmsg.Msg.Metadata._from_reply(ACK_SUBJECT.format(delivered=1))
    msg = _msg(MockMessage, metadata=real)

    await _deliver(Failing, msg)

    assert msg.nacked and not msg.terminated


async def test_CONTROL_a_handler_failure_at_the_limit_is_dead_lettered_and_terminated():
    real = natsmsg.Msg.Metadata._from_reply(ACK_SUBJECT.format(delivered=3))
    msg = _msg(MockMessage, metadata=real)

    async with ServiceTestHarness(Failing, config=_config()) as harness:
        await harness.container.dispatcher.jetstream.handle_jetstream_event(msg, pattern=None)
        published = [subject for subject, _, _ in harness.jetstream.published]

    assert msg.terminated and not msg.nacked
    assert published == ["dlq.probe_svc"], published

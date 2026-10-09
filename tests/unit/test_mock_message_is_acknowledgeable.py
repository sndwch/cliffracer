"""A message the suite's helper builds can be acknowledged.

The dispatcher acknowledges through `safe_ack`, which guards on the attribute:

    if hasattr(msg, "ack") and callable(msg.ack):

An envelope with no `ack` therefore takes neither the acknowledge branch nor the
except branch. `safe_ack` returns False, logs nothing, and raises nothing, so a
message that was never acknowledged is indistinguishable from one that was --
a quieter failure than a double acknowledgement, which at least warns.
"""

import logging

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit


def _dispatcher():
    svc = CliffracerService(ServiceConfig(name="ack_probe", health_port=0))
    return svc.container.dispatcher.jetstream


async def test_a_helper_built_message_can_be_acknowledged(test_helper):
    """The envelope the suite hands its tests carries the acknowledgement surface."""
    msg = test_helper.create_mock_message("orders.created", {"order_id": "o-1"})

    assert isinstance(msg, MockMessage)
    assert await _dispatcher().safe_ack(msg) is True
    assert msg.acked is True


async def test_the_helper_honours_the_reply_it_is_given():
    """The reply argument reaches `reply`, not the parameter that precedes it."""
    from tests.conftest import TestServiceHelper

    msg = TestServiceHelper.create_mock_message("orders.created", {}, reply="_INBOX.chosen")

    assert msg.reply == "_INBOX.chosen"
    assert msg.headers == {}, "the reply landed in headers, which is a different parameter"


async def test_CONTROL_an_envelope_without_ack_fails_silently(caplog):
    """What the old envelope did, pinned so the quiet path is written down.

    This is the behaviour the change above removes from the suite's own helper.
    It remains true of `safe_ack` for any object lacking `ack`, and it is worth
    an explicit test because nothing else in the tree says that this failure is
    silent rather than logged.
    """

    class NoAckEnvelope:
        subject = "orders.created"

    with caplog.at_level(logging.WARNING):
        acknowledged = await _dispatcher().safe_ack(NoAckEnvelope())

    assert acknowledged is False
    assert caplog.records == [], (
        "safe_ack logged something for a message with no ack; the point of this "
        "test is that it does not, so the miss is invisible"
    )

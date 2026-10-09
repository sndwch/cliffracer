"""A backend that raises has said nothing about the token; it is our fault.

`AuthExtension` converted ANY exception from the auth backend into
`RejectMessage("unauthenticated")`. Failing closed was right -- the handler must
not run when authentication could not be established. The label was not: an
unreachable issuer has not judged the token, which may be perfectly valid, so
the caller was sent to check credentials for an outage on our side.

On JetStream it was worse than a misleading label. A refusal is acknowledged,
and acknowledging destroys the message -- so for as long as the auth backend was
down, every event that arrived was silently consumed, with no dead-letter record
to replay from. Persistence is the whole reason those events were on a stream.

The extension now lets the exception out. `fails_closed` is True, so the
pipeline stops the handler exactly as before and synthesises
`RejectMessage(hook_crash=True)`; what changes is that the fault is reported as
ours and the event is redelivered rather than dropped.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cliffracer_auth import AuthExtension

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


class UnreachableIssuer:
    """Not a bad token: a backend that could not answer at all."""

    def validate_token(self, token):
        raise RuntimeError("issuer unreachable")


class _Svc(CliffracerService):
    auth = AuthExtension(UnreachableIssuer())

    @listener("events.ping", durable="pinger")
    async def on_ping(self, subject: str, seq: int = 0):
        self.seen.append(seq)


def _config():
    return ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        jetstream_max_deliver=5,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )


async def _deliver(num_delivered=1):
    svc = _Svc(_config())
    svc.seen = []
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    svc._discover_handlers()
    await svc.container._setup_extensions()
    assert svc.container.extensions, "fixture: the auth extension was not attached"

    msg = AsyncMock()
    msg.subject, msg.data = "events.ping", b'{"seq": 1}'
    # A token must be PRESENT, or `token is None` refuses before the backend is
    # ever consulted and these tests pass for an unrelated reason.
    msg.headers = {"authorization": "Bearer t"}
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    await svc.container._handle_jetstream_event(msg, pattern="events.ping")

    disposition = [
        n for n, c in (("ack", msg.ack), ("nak", msg.nak), ("term", msg.term)) if c.await_count
    ]
    dlq = [
        c
        for rec in (svc.nc, svc.js)
        for c in rec.publish.await_args_list
        if c.args and str(c.args[0]).startswith("dlq.")
    ]
    return SimpleNamespace(disposition=disposition, dlq=dlq, handled=svc.seen)


async def test_an_event_is_not_destroyed_while_the_auth_backend_is_down():
    """The issue's scenario: this was `['ack']`, and the event was gone."""
    out = await _deliver()

    assert out.disposition == ["nak"], out.disposition
    assert out.handled == [], "failing closed must still stop the handler"


async def test_the_event_reaches_the_dead_letter_queue_rather_than_nothing():
    """A backend down longer than `max_deliver` leaves something to replay."""
    out = await _deliver(num_delivered=5)

    assert out.disposition == ["term"], out.disposition
    assert len(out.dlq) == 1, f"expected a dead-letter record, got {out.dlq}"


async def test_CONTROL_a_token_the_backend_actually_judged_is_still_refused():
    """The half that must not move: a backend that ANSWERS "no" is a refusal.

    Without this, routing every auth failure to the crash path would pass the
    tests above and turn every bad token into a redelivery storm.
    """

    class Rejecting:
        def validate_token(self, token):
            return None  # answered: this token is not valid

    class Svc(CliffracerService):
        auth = AuthExtension(Rejecting())

        @listener("events.ping", durable="pinger")
        async def on_ping(self, subject: str, seq: int = 0):
            self.seen.append(seq)

    svc = Svc(_config())
    svc.seen = []
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    svc._discover_handlers()
    await svc.container._setup_extensions()

    msg = AsyncMock()
    msg.subject, msg.data = "events.ping", b'{"seq": 1}'
    msg.headers = {"authorization": "Bearer t"}
    msg.metadata = SimpleNamespace(num_delivered=1)
    await svc.container._handle_jetstream_event(msg, pattern="events.ping")

    assert msg.ack.await_count == 1, "a judged refusal is acknowledged, per ADR-0006"
    assert msg.nak.await_count == 0
    assert svc.seen == []

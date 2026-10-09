"""A hook that CRASHED is the service being broken; a refusal is a decision.

On JetStream the two need opposite dispositions and the path could not tell
them apart. Both arrive at `handle_event` as `RejectMessage`, so both were
acknowledged -- and acknowledging destroys the message. For a policy refusal
that is right and ADR-0006 says so: the message was seen, judged and turned
away, and redelivering it changes nothing. For a crashed `fails_closed` hook it
means a fault of ours silently consumes an event the stream exists to keep, and
a redelivery might well have cleared it.

`RejectMessage.hook_crash` already carries the distinction -- the pipeline sets
it and `rpc.py` reads it at both of its `refused:` arms -- so nothing here has to
classify anything. It reads the flag the pipeline set at the raise site.

The crash takes the path a handler exception already takes, rather than a new
one: nak with the configured backoff, then the DLQ and term once `max_deliver`
is spent. That is not a new policy, it is the existing one applied to a fault
that was being hidden.

ONE HARNESS, BOTH DIRECTIONS. The divergence is the whole subject, so a test
that only showed the crash would leave "and the refusal still acks" to a
different file with a different fixture --
`test_reject_message_caller_paths.py::test_a_refused_jetstream_event_is_acked_and_never_naked_or_terminated`
pins the refusal independently and must stay green, but a reader of this file
should be able to see both outcomes come out of one setup.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


class Refuses(Extension):
    """What an auth extension does for a token it has actually judged."""

    fails_closed = True

    async def worker_setup(self, ctx):
        raise RejectMessage("unauthenticated")


class Crashes(Extension):
    """What a `fails_closed` hook does when its backend is unreachable.

    It raises something that is not a `RejectMessage`; the pipeline converts it
    and marks `hook_crash`.
    """

    fails_closed = True

    async def worker_setup(self, ctx):
        raise RuntimeError("issuer unreachable")


class _Base(CliffracerService):
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


async def _deliver(ext, num_delivered=1):
    """Drive one JetStream delivery through the real dispatch path.

    Extensions are discovered as CLASS attributes, so the service type is built
    per call. An instance attribute set in `__init__` is not found, and the hook
    then never runs -- which reads as "the refusal did nothing" rather than as a
    broken fixture, so the assertion below states what must be true of the setup.
    """
    cls = type("Svc", (_Base,), {"ext0": ext})
    svc = cls(_config())
    svc.seen = []
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    svc._discover_handlers()
    await svc.container._setup_extensions()
    assert svc.container.extensions, "fixture: no extension was attached"

    msg = AsyncMock()
    msg.subject, msg.data, msg.headers = "events.ping", b'{"seq": 1}', None
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)

    await svc.container._handle_jetstream_event(msg, pattern="events.ping")

    disposition = [
        name
        for name, call in (("ack", msg.ack), ("nak", msg.nak), ("term", msg.term))
        if call.await_count
    ]
    # With `jetstream_enabled` the dead-letter publisher goes through `js`, not
    # `nc`. Reading only `nc` reports zero for a DLQ that did publish.
    dlq = [
        call
        for rec in (svc.nc, svc.js)
        for call in rec.publish.await_args_list
        if call.args and str(call.args[0]).startswith("dlq.")
    ]
    return SimpleNamespace(disposition=disposition, dlq=dlq, handled=svc.seen)


async def test_a_crashed_hook_is_redelivered_rather_than_acknowledged():
    """The defect: this was `['ack']`, and the event was gone."""
    out = await _deliver(Crashes())

    assert out.disposition == ["nak"], out.disposition
    assert out.handled == [], "a crashed fails_closed hook must still stop the handler"


async def test_a_crashed_hook_is_dead_lettered_once_max_deliver_is_spent():
    """The redeliveries have to end somewhere, and it is where they already end."""
    out = await _deliver(Crashes(), num_delivered=5)

    assert out.disposition == ["term"], out.disposition
    assert len(out.dlq) == 1, f"expected one dead-letter record, got {out.dlq}"


async def test_CONTROL_a_policy_refusal_is_still_acknowledged():
    """ADR-0006, unchanged, and the half that must not move.

    If this went red with the tests above green, the change would have replaced
    one indiscriminate disposition with another.
    """
    out = await _deliver(Refuses())

    assert out.disposition == ["ack"], out.disposition
    assert out.dlq == [], "a policy refusal is not a dead letter"
    assert out.handled == []


async def test_CONTROL_a_policy_refusal_is_acknowledged_at_every_delivery_count():
    """Not a redelivery that happens to look like an ack on the first attempt."""
    out = await _deliver(Refuses(), num_delivered=5)

    assert out.disposition == ["ack"], out.disposition
    assert out.dlq == []


# --- the same classification, where a test author meets it -------------------
#
# `ServiceTestHarness.emit_event` defaults to `raise_on_error=True`, and that
# switch is what the dispatch path reads to decide whether a crashed hook
# propagates. So the harness inherits the change, and a test whose extension
# crashed now hears about it instead of watching the event vanish.


class _CrashHarnessSvc(CliffracerService):
    ext0 = Crashes()

    @listener("events.ping", fanout=True)
    async def on_ping(self, subject: str, seq: int = 0):
        raise AssertionError("the handler must not run for a crashed fails_closed hook")


class _RefuseHarnessSvc(CliffracerService):
    ext0 = Refuses()

    @listener("events.ping", fanout=True)
    async def on_ping(self, subject: str, seq: int = 0):
        raise AssertionError("the handler must not run for a refused message")


async def test_the_harness_surfaces_a_crashed_hook_to_the_test():
    from cliffracer.testing import ServiceTestHarness

    async with ServiceTestHarness(_CrashHarnessSvc) as harness:
        with pytest.raises(RejectMessage) as exc:
            await harness.emit_event("events.ping", {"seq": 1})

    assert exc.value.hook_crash is True, "propagated, but not as the crash it is"


async def test_CONTROL_the_harness_does_not_surface_a_policy_refusal():
    """A refusal is a decision the service made, not an error it hit.

    Raising here would make every test of a refusing extension wrap its own
    delivery in `pytest.raises`, and would say the service failed when it did
    exactly what it was configured to do.
    """
    from cliffracer.testing import ServiceTestHarness

    async with ServiceTestHarness(_RefuseHarnessSvc) as harness:
        outcome = await harness.emit_event("events.ping", {"seq": 1})

    assert outcome is not None

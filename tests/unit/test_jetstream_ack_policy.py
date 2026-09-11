"""Handler returns -> ack. Raises -> nak, then DLQ and terminate. Invalid -> terminate."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js.api import AckPolicy
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.jetstream import StreamSpec, nak_delay


class Ping(BaseModel):
    seq: int


def _msg(subject="events.ping", data=b'{"seq": 1}', num_delivered=1):
    msg = AsyncMock()
    msg.subject = subject
    msg.data = data
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    return msg


class _Svc(CliffracerService):
    def __init__(self, config, fail=False):
        super().__init__(config)
        self._fail = fail
        self.seen = []

    @listener("events.ping", durable="pinger")
    async def on_ping(self, subject: str, seq: int = 0):
        if self._fail:
            raise RuntimeError("handler exploded")
        self.seen.append({"seq": seq})


class _ValidatedSvc(CliffracerService):
    @validated_listener("events.ping", Ping, durable="pinger")
    async def on_ping(self, message: Ping):
        pass


def _config(**overrides):
    return ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
        **overrides,
    )


def _mocked(svc):
    """Attach transport mocks and discover handlers on service."""
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc._discover_handlers()
    return svc


@pytest.mark.unit
class TestNakDelay:
    def test_first_delivery_waits_the_base_delay(self):
        assert nak_delay(1, _config()) == 1.0

    def test_backoff_doubles(self):
        assert nak_delay(2, _config()) == 2.0
        assert nak_delay(3, _config()) == 4.0

    def test_backoff_is_capped(self):
        assert nak_delay(20, _config(jetstream_max_backoff=10.0)) == 10.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_handler_acks():
    svc = _Svc(_config())
    _mocked(svc)

    msg = _msg()
    await svc.container._handle_jetstream_event(msg)

    assert msg.ack.await_count == 1
    assert msg.nak.await_count == 0
    assert msg.term.await_count == 0
    assert svc.seen == [{"seq": 1}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raising_handler_naks_with_backoff():
    svc = _Svc(_config(), fail=True)
    _mocked(svc)

    msg = _msg(num_delivered=2)
    await svc.container._handle_jetstream_event(msg)

    assert msg.ack.await_count == 0
    assert msg.term.await_count == 0
    assert msg.nak.await_count == 1
    assert msg.nak.call_args.kwargs["delay"] == 2.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exhausted_deliveries_dead_letter_then_terminate():
    svc = _Svc(_config(), fail=True)
    _mocked(svc)

    msg = _msg(num_delivered=5)  # == jetstream_max_deliver
    await svc.container._handle_jetstream_event(msg)

    assert msg.term.await_count == 1
    assert msg.nak.await_count == 0
    dlq_subjects = [c.args[0] for c in svc.js.publish.call_args_list]
    assert "dlq.pinger" in dlq_subjects


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failing_dead_letter_still_terminates():
    """Redelivering a message we cannot dead-letter forever is worse than losing it
    to a log line that carries the payload."""
    svc = _Svc(_config(), fail=True)
    _mocked(svc)
    svc.js.publish.side_effect = RuntimeError("stream full")

    msg = _msg(num_delivered=5)
    await svc.container._handle_jetstream_event(msg)  # must not raise

    assert msg.term.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_payload_terminates_without_nak():
    """Redelivering something that does not parse can never succeed."""
    svc = _ValidatedSvc(_config())
    _mocked(svc)

    msg = _msg(data=b'{"seq": "not-a-number"}')
    await svc.container._handle_jetstream_event(msg)

    assert msg.term.await_count == 1
    assert msg.nak.await_count == 0
    assert msg.ack.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_listener_binds_a_jetstream_consumer_with_a_deliver_group():
    svc = _Svc(_config())
    _mocked(svc)
    await svc.container._setup_subscriptions()

    assert svc.js.subscribe.await_count == 1
    call = svc.js.subscribe.call_args
    assert call.args[0] == "events.ping"
    assert call.kwargs["durable"] == "pinger"
    # Deliver group == durable name: two replicas share one consumer, so a
    # message is processed once across the pair rather than twice.
    assert call.kwargs["queue"] == "pinger"
    assert call.kwargs["manual_ack"] is True
    assert call.kwargs["config"].max_deliver == 5
    # manual_ack=True combined with anything but explicit ack policy is a
    # silent auto-ack: the server acks on delivery and redelivery never happens.
    assert call.kwargs["config"].ack_policy is AckPolicy.EXPLICIT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_listener_without_durable_stays_on_core_nats():
    """A JetStream-enabled service can mix both. durable= is the only switch."""

    class _Mixed(CliffracerService):
        @listener("events.ping", durable="pinger")
        async def durable_one(self, subject: str) -> None:
            pass

        @listener("events.pong", fanout=True)
        async def core_one(self, subject: str) -> None:
            pass

    svc = _Mixed(_config())
    _mocked(svc)
    await svc.container._setup_subscriptions()

    js_subjects = [c.args[0] for c in svc.js.subscribe.call_args_list]
    core_subjects = [c.args[0] for c in svc.nc.subscribe.call_args_list]
    assert js_subjects == ["events.ping"]
    assert "events.pong" in core_subjects


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durable_is_keyed_by_the_effective_subject():
    """cross_namespace resolves to *.pattern, and the consumer must filter on that."""

    class _Cross(CliffracerService):
        @listener("events.ping", cross_namespace=True, durable="pinger")
        async def on_ping(self, subject: str) -> None:
            pass

    svc = _Cross(_config(namespace="utils"))
    _mocked(svc)
    await svc.container._setup_subscriptions()

    assert svc.js.subscribe.call_args.args[0] == "*.events.ping"

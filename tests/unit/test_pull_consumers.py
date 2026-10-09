"""Unit tests verifying JetStream pull consumer backpressure and configuration constraints."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


def _config(**overrides):
    return ServiceConfig(
        name="puller",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
        **overrides,
    )


def _msg(subject="events.ping", data=b'{"seq": 1}'):
    msg = AsyncMock()
    msg.subject = subject
    msg.data = data
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=1)
    return msg


def test_pull_requires_a_durable():
    """A pull consumer requires an explicit durable consumer name."""

    class S(CliffracerService):
        @listener("events.ping", pull=True)
        async def on_ping(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    assert "declares pull=True with no durable" in str(exc.value)
    assert "A pull consumer IS a durable consumer" in str(exc.value)


def test_pull_requires_jetstream():
    """Pull consumers require jetstream_enabled=True."""

    class S(CliffracerService):
        @listener("events.ping", durable="pinger", pull=True)
        async def on_ping(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(ServiceConfig(name="puller", jetstream_enabled=False))._discover_handlers()

    assert "declares pull=True but this service has jetstream_enabled=False" in str(exc.value)
    assert "Core NATS has no pull consumers" in str(exc.value)


def test_pull_and_fanout_are_exclusive():
    class S(CliffracerService):
        @listener("events.ping", durable="pinger", pull=True, fanout=True)
        async def on_ping(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    assert "declares both pull=True and fanout=True" in str(exc.value)


def test_a_pull_listener_binds_a_pull_subscription_not_a_push_one():
    class S(CliffracerService):
        @listener("events.ping", durable="pinger", pull=True)
        async def on_ping(self, subject: str) -> None:
            pass

    svc = S(_config())
    svc._discover_handlers()
    svc.nc = AsyncMock()
    svc.js = AsyncMock()

    import asyncio

    asyncio.run(svc.container.setup_subscriptions())

    assert svc.js.pull_subscribe.await_count == 1, "must use pull_subscribe"
    assert svc.js.subscribe.await_count == 0, "must not also push-subscribe"
    kwargs = svc.js.pull_subscribe.await_args.kwargs
    assert kwargs["durable"] == "pinger"


def test_the_pull_consumer_is_bounded_per_replica():
    """max_ack_pending is the per-replica in-flight bound, and it is what makes
    a busy replica stop taking work."""

    class S(CliffracerService):
        @listener("events.ping", durable="pinger", pull=True)
        async def on_ping(self, subject: str) -> None:
            pass

    svc = S(_config(jetstream_max_ack_pending=7))
    svc._discover_handlers()
    svc.nc = AsyncMock()
    svc.js = AsyncMock()

    import asyncio

    asyncio.run(svc.container.setup_subscriptions())

    config = svc.js.pull_subscribe.await_args.kwargs["config"]
    assert config.max_ack_pending == 7


def test_pull_once_dispatches_every_fetched_message():
    """`_pull_once` hands each message of a fetched batch to the shared handler.

    The handler is replaced here, so this says nothing about what happens to a
    message afterwards; the tests below run the real one.
    """

    class S(CliffracerService):
        @listener("events.ping", durable="pinger", pull=True)
        async def on_ping(self, subject: str, seq: int = 1) -> None:
            pass

    svc = S(_config())
    svc._discover_handlers()
    handled = []
    # Container owns dispatch and routes to _handle_jetstream_event.
    svc.container._handle_jetstream_event = AsyncMock(side_effect=lambda m: handled.append(m))

    sub = AsyncMock()
    sub.fetch = AsyncMock(side_effect=[[_msg(), _msg()], StopAsyncIteration()])

    import asyncio

    async def run_once():
        await svc.container._pull_once(sub)

    asyncio.run(run_once())

    assert len(handled) == 2, "every fetched message goes through the shared handler"


def _pulling_service(*, fails=False, **config):
    class S(CliffracerService):
        @listener("events.ping", durable="pinger", pull=True)
        async def on_ping(self, subject: str, seq: int = 1) -> None:
            if fails:
                raise ValueError("poison")

    svc = S(_config(**config))
    svc._discover_handlers()
    svc.nc = AsyncMock()
    svc.js = AsyncMock()
    return svc


async def _pull(svc, *messages):
    sub = AsyncMock()
    sub.fetch = AsyncMock(return_value=list(messages))
    await svc.container._pull_once(sub)


@pytest.mark.asyncio
async def test_a_pulled_message_that_is_handled_is_acked_and_nothing_else():
    svc = _pulling_service()
    first, second = _msg(), _msg()

    await _pull(svc, first, second)

    for msg in (first, second):
        msg.ack.assert_awaited_once()
        msg.nak.assert_not_awaited()
        msg.term.assert_not_awaited()
    assert svc.js.publish.await_count == svc.nc.publish.await_count == 0


@pytest.mark.asyncio
async def test_a_pulled_message_that_fails_before_its_last_delivery_is_naked_not_dead_lettered():
    svc = _pulling_service(fails=True, jetstream_max_deliver=5)
    msg = _msg()
    msg.metadata = SimpleNamespace(num_delivered=2)

    await _pull(svc, msg)

    msg.nak.assert_awaited_once()
    msg.ack.assert_not_awaited()
    msg.term.assert_not_awaited()
    assert svc.js.publish.await_count == svc.nc.publish.await_count == 0


@pytest.mark.asyncio
async def test_a_pulled_message_that_fails_its_last_delivery_is_dead_lettered_and_terminated():
    svc = _pulling_service(fails=True, jetstream_max_deliver=5)
    msg = _msg()
    msg.metadata = SimpleNamespace(num_delivered=5)

    await _pull(svc, msg)

    msg.term.assert_awaited_once()
    msg.ack.assert_not_awaited()
    msg.nak.assert_not_awaited()
    published = [c.args[0] for c in svc.js.publish.await_args_list + svc.nc.publish.await_args_list]
    assert len(published) == 1
    assert published[0].startswith("dlq."), published

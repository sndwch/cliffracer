"""Unit tests verifying JetStream pull consumer backpressure and configuration constraints."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec


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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
def test_pull_and_fanout_are_exclusive():
    class S(CliffracerService):
        @listener("events.ping", durable="pinger", pull=True, fanout=True)
        async def on_ping(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    assert "declares both pull=True and fanout=True" in str(exc.value)


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
def test_fetched_messages_go_through_the_same_ack_and_dlq_path():
    """Verify fetched messages route through the shared handler and ack/dlq logic."""

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

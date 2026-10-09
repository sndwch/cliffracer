"""The harness runs a JetStream service's JetStream paths, rather than around them."""

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec
from cliffracer.testing import MockMessage, ServiceTestHarness

pytestmark = pytest.mark.unit


class Publisher(CliffracerService):
    @rpc
    async def send_declared(self) -> dict[str, bool]:
        await self.publish_event("probe.declared.thing", value=1)
        return {"published": True}

    @rpc
    async def send_undeclared(self) -> dict[str, bool]:
        await self.publish_event("probe.undeclared", value=1)
        return {"published": True}


class Consumer(CliffracerService):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.seen: list[int] = []

    @listener("probe.declared.thing", durable="prober")
    async def on_thing(self, subject: str, value: int = 0) -> None:
        if value < 0:
            raise RuntimeError("handler exploded")
        self.seen.append(value)


def _config(name: str = "probe_svc", **overrides) -> ServiceConfig:
    return ServiceConfig(
        name=name,
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="DECLARED", subjects=["probe.declared.>"]),
            StreamSpec(name="DLQ", subjects=["dlq.>"]),
        ],
        **overrides,
    )


async def test_a_jetstream_service_publishes_over_jetstream_under_the_harness():
    """The harness leaves JetStream on, so the JetStream publish path is the one taken."""
    async with ServiceTestHarness(Publisher, config=_config()) as harness:
        assert harness.container._jetstream_active, (
            "JetStream is off under the harness, so every JetStream-only guard is skipped "
            "and a green test says nothing about a service that uses JetStream"
        )
        response = await harness.rpc("send_declared")
        assert response.success

        assert harness.jetstream.published_subjects == ["probe.declared.thing"]


async def test_publishing_to_an_undeclared_subject_fails_under_the_harness():
    """The guard that a real broker applies is applied here too.

    Without it a service that publishes to a subject no declared stream covers
    passes every harness test and raises the moment it meets a broker.
    """
    async with ServiceTestHarness(Publisher, config=_config()) as harness:
        with pytest.raises(StreamDeclarationError):
            await harness.service.publish_event("probe.undeclared", value=1)


async def test_an_rpc_publishing_to_an_undeclared_subject_no_longer_reports_success():
    """The reported symptom: a call that is fatal against a broker answered success.

    The RPC path turns the guard's exception into an error response rather than
    raising, so this reads the response the way a test author would.
    """
    async with ServiceTestHarness(Publisher, config=_config()) as harness:
        response = await harness.rpc("send_undeclared")

        assert response.success is False
        assert harness.jetstream.published_subjects == [], (
            "the undeclared subject reached JetStream despite the guard"
        )


async def test_deliver_jetstream_acknowledges_a_handled_message():
    """A delivery that a handler accepts is acked, and the message is returned."""
    async with ServiceTestHarness(Consumer, config=_config("consumer_svc")) as harness:
        msg = await harness.deliver_jetstream("probe.declared.thing", {"value": 7})

        assert isinstance(msg, MockMessage)
        assert harness.service.seen == [7]
        assert msg.acked is True
        assert msg.terminated is False
        assert msg.nacked is False


async def test_deliver_jetstream_naks_a_handler_failure_that_has_deliveries_left():
    """A first failure is redelivered rather than terminated."""
    async with ServiceTestHarness(Consumer, config=_config("consumer_svc")) as harness:
        msg = await harness.deliver_jetstream("probe.declared.thing", {"value": -1})

        assert msg.nacked is True
        assert msg.acked is False
        assert msg.terminated is False


async def test_deliver_jetstream_terminates_once_deliveries_are_exhausted():
    """num_delivered is reachable from the harness, so the retry policy is testable."""
    config = _config("consumer_svc", jetstream_max_deliver=3)
    async with ServiceTestHarness(Consumer, config=config) as harness:
        msg = await harness.deliver_jetstream(
            "probe.declared.thing", {"value": -1}, num_delivered=3
        )

        assert msg.terminated is True
        assert msg.nacked is False
        assert msg.acked is False


class CorePublisher(CliffracerService):
    """A service with JetStream off: every publish takes the core path."""

    @rpc
    async def send(self) -> dict[str, bool]:
        await self.publish_event("probe.declared.thing", value=1)
        return {"published": True}

    @listener("probe.declared.thing", fanout=True)
    async def on_thing(self, subject: str, value: int = 0) -> None:
        return None


def _core_config() -> ServiceConfig:
    """The same service, with JetStream off and no streams to declare."""
    return ServiceConfig(name="core_svc", health_port=0, jetstream_enabled=False)


async def test_the_jetstream_context_is_refused_when_the_service_has_jetstream_off():
    """An empty publish history is not evidence about JetStream.

    With JetStream off the context is constructed but never attached to the
    container, so it reports an empty history whatever the service does. Handing
    it back makes `published == []` read as "nothing went out over JetStream"
    when the truth is that there was no JetStream, and makes the opposite
    assertion unpassable.
    """
    async with ServiceTestHarness(CorePublisher, config=_core_config()) as harness:
        assert harness.container._jetstream_active is False

        with pytest.raises(RuntimeError, match="jetstream_enabled=False"):
            harness.jetstream  # noqa: B018


async def test_deliver_jetstream_is_refused_when_the_service_has_jetstream_off():
    """The JetStream dispatch path is not reachable on a service that has none.

    Driving it anyway lets a test assert the retry and dead-letter policy of a
    service that would never take that path in the configuration under test.
    """
    async with ServiceTestHarness(CorePublisher, config=_core_config()) as harness:
        with pytest.raises(RuntimeError, match="jetstream_enabled=False"):
            await harness.deliver_jetstream("probe.declared.thing", {"value": 1})


async def test_CONTROL_the_refusal_names_the_surface_it_declined():
    """Two surfaces, one message: each says which one was asked."""
    async with ServiceTestHarness(CorePublisher, config=_core_config()) as harness:
        with pytest.raises(RuntimeError, match="harness.jetstream answers for JetStream"):
            harness.jetstream  # noqa: B018

        with pytest.raises(RuntimeError, match="deliver_jetstream answers for JetStream"):
            await harness.deliver_jetstream("probe.declared.thing", {"value": 1})


async def test_CONTROL_a_service_with_jetstream_off_still_dispatches_its_core_path():
    """The refusal is scoped to the JetStream surfaces, not to the harness.

    Without this a refusal that fired on every path would look like a pass here.
    """
    async with ServiceTestHarness(CorePublisher, config=_core_config()) as harness:
        response = await harness.rpc("send")

        assert response.success is True
        assert response.data["result"] == {"published": True}


async def test_CONTROL_both_surfaces_still_answer_when_jetstream_is_on():
    """And the refusal is conditional: with JetStream on, both answer as before."""
    async with ServiceTestHarness(Consumer, config=_config("consumer_svc")) as harness:
        msg = await harness.deliver_jetstream("probe.declared.thing", {"value": 7})

        assert msg.acked is True
        assert harness.jetstream.published_subjects == []

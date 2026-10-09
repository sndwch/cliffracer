"""A durable listener whose subject no declared stream carries is refused before startup connects."""

from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec
from cliffracer.core.registry import ServiceRegistry

pytestmark = pytest.mark.unit


class Order(BaseModel):
    order_id: str


def _config(*claims: str, **overrides) -> ServiceConfig:
    streams = [StreamSpec(name=f"S{i}", subjects=[claim]) for i, claim in enumerate(claims)]
    return ServiceConfig(
        name="orders",
        jetstream_enabled=True,
        jetstream_streams=streams,
        dlq_subject="dlq.orders",
        **overrides,
    )


class Uncovered(CliffracerService):
    started: list[str]

    @listener("orders.created", durable="orders-worker")
    async def on_order(self, subject: str) -> None:
        pass

    async def on_startup(self) -> None:
        self.started.append("on_startup")


@pytest.mark.asyncio
async def test_start_refuses_before_it_connects_or_runs_anything():
    """The failure used to arrive at the last step, as the server's bare "not found", after
    `on_startup` had run."""
    service = Uncovered(_config("dlq.orders"))
    service.started = []
    service.connect = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(StreamDeclarationError) as caught:
        await service.start()

    message = str(caught.value)
    assert "'orders.created'" in message, message
    assert "'orders-worker'" in message, message
    assert "'on_order'" in message, message
    assert "['dlq.orders']" in message, message
    service.connect.assert_not_awaited()
    assert service.started == []


def test_every_uncovered_durable_is_listed_not_only_the_first():
    class Two(CliffracerService):
        @listener("orders.created", durable="a")
        async def on_created(self, subject: str) -> None:
            pass

        @validated_listener("orders.shipped", Order, durable="b")
        async def on_shipped(self, message: Order) -> None:
            pass

    service = Two(_config("dlq.orders"))
    service.container.discover_handlers()

    with pytest.raises(StreamDeclarationError) as caught:
        HandlerDiscovery.validate_durable_coverage(service.container.registry, service.config)

    message = str(caught.value)
    assert "2 durable listener(s)" in message, message
    assert "'orders.created'" in message and "'orders.shipped'" in message, message


@pytest.mark.parametrize("claim", ["orders.created", "orders.*", "orders.>"], ids=repr)
def test_CONTROL_a_declared_stream_that_carries_the_subject_is_accepted(claim):
    service = Uncovered(_config(claim, "dlq.orders"))
    service.container.discover_handlers()

    HandlerDiscovery.validate_durable_coverage(service.container.registry, service.config)


def test_CONTROL_a_pull_durable_is_judged_the_same_way():
    class Pull(CliffracerService):
        @listener("orders.created", durable="orders-pull", pull=True)
        async def on_order(self, subject: str) -> None:
            pass

    covered = Pull(_config("orders.>"))
    covered.container.discover_handlers()
    HandlerDiscovery.validate_durable_coverage(covered.container.registry, covered.config)

    uncovered = Pull(_config("dlq.orders"))
    uncovered.container.discover_handlers()
    with pytest.raises(StreamDeclarationError, match="orders-pull"):
        HandlerDiscovery.validate_durable_coverage(uncovered.container.registry, uncovered.config)


def test_a_namespaced_listener_is_judged_by_the_subject_it_subscribes_to():
    """With a namespace the subscription is `prod.orders.created`, so a stream for the bare
    subject does not carry it."""
    service = Uncovered(_config("orders.>", namespace="prod"))
    service.container.discover_handlers()

    with pytest.raises(StreamDeclarationError, match="prod.orders.created"):
        HandlerDiscovery.validate_durable_coverage(service.container.registry, service.config)

    covered = Uncovered(_config("prod.orders.>", namespace="prod"))
    covered.container.discover_handlers()
    HandlerDiscovery.validate_durable_coverage(covered.container.registry, covered.config)


def test_CONTROL_with_jetstream_off_a_durable_with_no_stream_is_not_this_checks_business():
    """The exemption itself: the same uncovered durable, with JetStream off, is not refused here.

    Discovery refuses a durable on a JetStream-off service for its own reason, so the registry
    is filled by hand to reach this check with that combination. Without the early return it
    would find no stream and refuse.
    """
    registry = ServiceRegistry()
    registry.event_durables["orders.created"] = "orders-worker"
    registry.event_handler_names["orders.created"] = "on_order"
    config = ServiceConfig(name="orders", jetstream_enabled=False)

    HandlerDiscovery.validate_durable_coverage(registry, config)

    # and the same registry under JetStream-on is refused, so the exemption is what spared it
    with pytest.raises(StreamDeclarationError, match="orders-worker"):
        HandlerDiscovery.validate_durable_coverage(
            registry, config.model_copy(update={"jetstream_enabled": True})
        )


def test_CONTROL_a_fanout_listener_with_no_durable_needs_no_stream():
    class Fanout(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    service = Fanout(_config("dlq.orders"))
    service.container.discover_handlers()

    HandlerDiscovery.validate_durable_coverage(service.container.registry, service.config)

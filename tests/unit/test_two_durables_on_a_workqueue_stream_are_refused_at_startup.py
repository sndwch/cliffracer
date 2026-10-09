"""Two of a service's durable listeners that overlap on a workqueue stream are refused before it connects.

A workqueue stream delivers each message to one consumer, so the server refuses a second durable
whose filter overlaps the first: `err_code=10100`, "filtered consumer not unique on workqueue
stream". That arrived as a bare broker error at the last step of startup, naming neither the
stream, the subjects nor the handlers. The overlap is known from discovery, so it is refused first.
"""

from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec
from cliffracer.core.subjects import subjects_overlap

pytestmark = pytest.mark.unit

# What nats-server 2.10.29 answered for two durables on one workqueue stream, one filtered on each
# subject, measured on a disposable broker: True is `err_code=10100` on the second.
BROKER_REFUSES = [
    ("a.b", "a.b", True),
    ("a.*", "a.b", True),
    ("a.b", "a.c", False),
    ("a.*", "a.*", True),
    ("a.>", "a.b.c", True),
    ("a.*", "a.b.c", False),
    ("a.*.z", "a.y.*", True),
    ("a.*.z", "a.y.w", False),
    ("a.>", "b.c", False),
    ("a.b.>", "a.b", False),
    ("*.x", "a.*", True),
    ("*.x", "a.y", False),
]


def _config(retention: str = "workqueue", **overrides) -> ServiceConfig:
    return ServiceConfig(
        name="orders",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["orders.>"], retention=retention),  # type: ignore[arg-type]
            StreamSpec(name="DLQ", subjects=["dlq.orders"]),
        ],
        dlq_subject="dlq.orders",
        **overrides,
    )


class Overlapping(CliffracerService):
    @listener("orders.*", durable="all-orders")
    async def on_any(self, subject: str) -> None:
        pass

    @listener("orders.created", durable="created-orders")
    async def on_created(self, subject: str) -> None:
        pass


class Disjoint(CliffracerService):
    @listener("orders.created", durable="created-orders")
    async def on_created(self, subject: str) -> None:
        pass

    @listener("orders.shipped", durable="shipped-orders")
    async def on_shipped(self, subject: str) -> None:
        pass


def _validate(service: CliffracerService) -> None:
    service.container.discover_handlers()
    HandlerDiscovery.validate_workqueue_consumers(service.container.registry, service.config)


@pytest.mark.asyncio
async def test_start_refuses_before_it_connects_and_names_the_stream_subjects_and_handlers():
    service = Overlapping(_config())
    service.connect = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(StreamDeclarationError) as caught:
        await service.start()

    message = str(caught.value)
    for named in (
        "'EVENTS'",
        "retention='workqueue'",
        "'orders.*'",
        "'orders.created'",
        "'all-orders'",
        "'created-orders'",
        "'on_any'",
        "'on_created'",
    ):
        assert named in message, (named, message)
    service.connect.assert_not_awaited()


def test_durables_on_subjects_that_do_not_overlap_are_accepted():
    _validate(Disjoint(_config()))


@pytest.mark.parametrize("retention", ["limits", "interest"])
def test_the_same_overlap_is_accepted_on_a_stream_that_allows_many_consumers(retention):
    _validate(Overlapping(_config(retention)))


def test_the_check_is_off_when_jetstream_is_off():
    """Given a config that leaves JetStream off, no consumer is created, so nothing can clash."""
    on = Overlapping(_config())
    on.container.discover_handlers()
    off = _config()
    off.jetstream_enabled = False

    HandlerDiscovery.validate_workqueue_consumers(on.container.registry, off)


def test_an_overlap_on_another_stream_is_not_judged_by_the_workqueue_stream():
    """Only the durables a workqueue stream hosts count: these two overlap on a `limits` stream."""

    class Billing(CliffracerService):
        @listener("billing.*", durable="all-billing")
        async def on_any(self, subject: str) -> None:
            pass

        @listener("billing.created", durable="created-billing")
        async def on_created(self, subject: str) -> None:
            pass

    config = ServiceConfig(
        name="orders",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["orders.>"], retention="workqueue"),
            StreamSpec(name="BILLING", subjects=["billing.>"]),
            StreamSpec(name="DLQ", subjects=["dlq.orders"]),
        ],
        dlq_subject="dlq.orders",
    )

    _validate(Billing(config))


def test_a_lone_durable_on_a_workqueue_stream_is_accepted():
    class One(CliffracerService):
        @listener("orders.*", durable="all-orders")
        async def on_any(self, subject: str) -> None:
            pass

    _validate(One(_config()))


def test_a_listener_with_no_durable_makes_no_consumer_and_clashes_with_nothing():
    class Mixed(CliffracerService):
        @listener("orders.*", durable="all-orders")
        async def on_any(self, subject: str) -> None:
            pass

        @listener("orders.created", fanout=True)
        async def on_created(self, subject: str) -> None:
            pass

    _validate(Mixed(_config()))


def test_every_clashing_pair_is_listed_not_only_the_first():
    class Three(CliffracerService):
        @listener("orders.>", durable="d-all")
        async def a(self, subject: str) -> None:
            pass

        @listener("orders.created", durable="d-created")
        async def b(self, subject: str) -> None:
            pass

        @listener("orders.shipped", durable="d-shipped")
        async def c(self, subject: str) -> None:
            pass

    with pytest.raises(StreamDeclarationError) as caught:
        _validate(Three(_config()))

    message = str(caught.value)
    assert "2 pair(s)" in message, message
    assert "'d-created'" in message and "'d-shipped'" in message and "'d-all'" in message, message


@pytest.mark.parametrize(("first", "second", "refused"), BROKER_REFUSES)
def test_the_overlap_the_check_uses_is_the_one_the_broker_refuses(first, second, refused):
    """The predicate agrees with the server on every measured pair, wildcards included."""
    assert subjects_overlap(first, second) is refused

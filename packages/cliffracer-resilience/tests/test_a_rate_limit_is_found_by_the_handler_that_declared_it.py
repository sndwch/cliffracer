"""A rate limit applies to the handler that declared it, and to no other handler.

The extension found a handler's limit by the handler's name, and then by the SUBJECT of the
message when the name had none. The subject map was keyed by the pattern a limited listener
declared, so another handler that received the same subject (an unlimited `orders.*` listener
beside a limited `orders.created` one) took the limited handler's budget and was refused in its
place. Every dispatch carries the handler's name, so the subject lookup was never needed for
the handler it was written for, and only ever answered for the wrong one. An RPC subject was read
the same way, by cutting `<service>.rpc.<name>` apart, and RPC dispatch names its handler too.
"""

import pytest
from cliffracer_resilience import RateLimitConfig, ResilienceExtension, rate_limit

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.extension import WorkerContext
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


class Orders(CliffracerService):
    resilience = ResilienceExtension()

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="orders_svc", subject_prefix=None))
        self.handled: list[str] = []

    @listener("orders.created", fanout=True)
    @rate_limit(calls=1, window=60)
    async def on_created(self, order_id: str) -> None:
        self.handled.append(f"limited:{order_id}")

    @listener("orders.*", fanout=True)
    async def on_any_order(self, order_id: str) -> None:
        self.handled.append(f"unlimited:{order_id}")


async def _started() -> Orders:
    service = Orders()
    await service.container._setup_extensions()
    service._discover_handlers()
    return service


async def _deliver(service: Orders, pattern: str, order_id: str) -> None:
    message = MockMessage(
        subject="orders.created", data=f'{{"order_id":"{order_id}"}}'.encode(), headers={}
    )
    await service.container.dispatcher.events.handle_event(message, pattern=pattern)


async def test_an_unlimited_handler_is_not_refused_by_another_handlers_limit():
    service = await _started()

    for order_id in ("o1", "o2", "o3"):
        await _deliver(service, "orders.*", order_id)

    assert service.handled == ["unlimited:o1", "unlimited:o2", "unlimited:o3"]


async def test_CONTROL_the_limited_handler_keeps_its_own_limit():
    service = await _started()

    for order_id in ("o1", "o2"):
        await _deliver(service, "orders.created", order_id)

    assert service.handled == ["limited:o1"]


async def test_both_handlers_in_one_service_are_limited_independently():
    service = await _started()

    await _deliver(service, "orders.created", "o1")
    await _deliver(service, "orders.*", "o1")
    await _deliver(service, "orders.created", "o2")
    await _deliver(service, "orders.*", "o2")

    assert service.handled == ["limited:o1", "unlimited:o1", "unlimited:o2"]


async def test_the_extension_keeps_no_map_keyed_by_subject():
    service = await _started()

    assert not hasattr(service.resilience, "_event_rate_limits")
    assert set(service.resilience._rate_limits) == {"on_created"}


async def test_a_context_that_names_no_handler_is_not_matched_by_its_subject():
    """The old fallback: a context with a subject equal to a declared pattern and no handler name."""
    service = await _started()
    context = WorkerContext(
        kind="event",
        subject="orders.created",
        headers={},
        correlation_id=None,
        payload={},
    )

    assert service.resilience._get_config(context) is None


async def test_a_context_that_names_no_handler_is_not_matched_by_an_rpc_subject():
    service = await _started()
    service.resilience._rate_limits["charge"] = RateLimitConfig(calls=1, window=60)
    context = WorkerContext(
        kind="rpc",
        subject="orders_svc.rpc.charge",
        headers={},
        correlation_id=None,
        payload={},
    )

    assert service.resilience._get_config(context) is None


async def test_CONTROL_a_named_handler_without_a_limit_gets_the_default_limit():
    """The path a named handler with no limit of its own falls to: the extension's default."""

    class Defaulted(Orders):
        resilience = ResilienceExtension(default_calls=1, default_window=60)

    service = Defaulted()
    await service.container._setup_extensions()
    service._discover_handlers()

    for order_id in ("o1", "o2"):
        await _deliver(service, "orders.*", order_id)

    assert service.handled == ["unlimited:o1"]

"""Verify `start()` flushes subscriptions before returning.

`nc.subscribe()` queues subscriptions on the connection buffer. `start()` flushes
the connection so subscriptions are registered with the broker before returning,
ensuring caller requests do not race against pending registrations.

Two halves: `setup_subscriptions()` ends in a flush (the first tests), and `start()` goes through
`setup_subscriptions()` at all, before it returns (the last test, which calls `start()`). The
caller-race argument needs both: a setup that flushes is no use to a start that never calls it.
"""

from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc

pytestmark = pytest.mark.unit


class _Svc(CliffracerService):
    @rpc
    async def echo(self, value: str) -> str:
        return value

    @listener("things.happened", fanout=True)
    async def on_thing(self) -> None:
        pass


def _service_with_a_recording_connection():
    svc = _Svc(ServiceConfig(name="flushy", health_port=0))
    svc._discover_handlers()
    nc = AsyncMock()
    nc.subscribe = AsyncMock(return_value=AsyncMock())
    svc.container.nc = nc
    return svc, nc


async def test_every_subscribe_is_flushed_before_setup_returns():
    svc, nc = _service_with_a_recording_connection()

    await svc.container.setup_subscriptions()

    names = [call[0] for call in nc.method_calls]
    assert "subscribe" in names, names
    assert names[-1] == "flush", (
        "the last thing setup does must be the flush, or a subscribe issued "
        f"after it reaches the broker later than the caller thinks: {names}"
    )
    assert names.index("flush") > max(i for i, n in enumerate(names) if n == "subscribe")


async def test_CONTROL_the_recorder_sees_the_subscribes_it_is_counting():
    """Without this, an empty `method_calls` would satisfy the ordering
    assertion above by vacuous truth -- there would be no subscribe to come
    before the flush."""
    svc, nc = _service_with_a_recording_connection()

    await svc.container.setup_subscriptions()

    subjects = [call.args[0] for call in nc.subscribe.await_args_list]
    assert "flushy.rpc.*" in subjects, subjects
    assert "flushy.async.*" in subjects, subjects
    assert "flushy.describe" in subjects, subjects
    assert "things.happened" in subjects, subjects


async def test_start_sets_up_and_flushes_the_subscriptions_before_it_returns(monkeypatch):
    """Through `start()` itself: when it returns, every subscription has been issued and the last
    thing done to the connection was the flush, so a request arriving the moment `start()` returns
    finds the subscriptions registered. A startup sequence that skipped the subscription step
    would leave `setup_subscriptions()` correct and this empty."""
    svc, nc = _service_with_a_recording_connection()
    svc.container._handlers_discovered = False  # `start()` discovers them itself
    svc.container.registry.clear()

    async def fake_connect():
        svc.container.nc = nc

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(svc, "connect", fake_connect)
    monkeypatch.setattr(svc, "disconnect", noop)

    await svc.start()
    try:
        subjects = [call.args[0] for call in nc.subscribe.await_args_list]
        assert {"flushy.rpc.*", "flushy.async.*", "flushy.describe", "things.happened"} <= set(
            subjects
        ), subjects
        names = [call[0] for call in nc.method_calls]
        assert names[-1] == "flush", names
        assert names.index("flush") > max(i for i, n in enumerate(names) if n == "subscribe")
    finally:
        await svc.stop()

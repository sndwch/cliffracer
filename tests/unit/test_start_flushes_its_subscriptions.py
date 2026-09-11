"""Verify `start()` flushes subscriptions before returning.

`nc.subscribe()` queues subscriptions on the connection buffer. `start()` flushes
the connection so subscriptions are registered with the broker before returning,
ensuring caller requests do not race against pending registrations.
"""

from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc


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


@pytest.mark.unit
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


@pytest.mark.unit
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

"""A pooled connection keeps reconnecting for as long as its service does.

nats-py closes a client for good once its reconnect attempts run out. The
service connects with `ServiceConfig.max_reconnect_attempts`, which is -1
(forever) by default, but the pool used its own default of 10 attempts one
second apart. A broker outage of about fifteen seconds then left the service
reconnected and healthy while every pooled client was closed for good, and
`pool.request()` raised `ConnectionClosedError` until the process restarted.

These tests read what the pool passes to `nats.connect`, so they need no
broker. The reconnect policy is nats-py's; what is pinned here is that the pool
asks for the policy its service does, and that an explicit value still wins.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from cliffracer_metrics import PoolExtension

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


async def _connect_kwargs(monkeypatch, pool: PoolExtension, **config) -> list[dict]:
    """The keyword arguments of every `nats.connect` the pool makes at start."""
    connect = AsyncMock(side_effect=lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    class Svc(CliffracerService):
        pooled = pool

    svc = Svc(ServiceConfig(name="svc", health_port=0, **config))
    await svc.container._setup_extensions()
    await svc.pooled.start()
    return [call.kwargs for call in connect.await_args_list]


async def test_the_pool_follows_the_services_default_reconnect_policy(monkeypatch):
    calls = await _connect_kwargs(monkeypatch, PoolExtension(max_connections=2))

    assert len(calls) == 2
    for kwargs in calls:
        assert kwargs["max_reconnect_attempts"] == -1, (
            "the pool gives up on a broker the service keeps"
        )
        assert kwargs["reconnect_time_wait"] == 2


async def test_the_pool_follows_a_services_configured_reconnect_policy(monkeypatch):
    calls = await _connect_kwargs(
        monkeypatch,
        PoolExtension(max_connections=1),
        max_reconnect_attempts=7,
        reconnect_time_wait=5,
    )

    assert calls[0]["max_reconnect_attempts"] == 7
    assert calls[0]["reconnect_time_wait"] == 5


async def test_an_explicit_value_on_the_extension_still_wins(monkeypatch):
    calls = await _connect_kwargs(
        monkeypatch,
        PoolExtension(max_connections=1, max_reconnect_attempts=3, reconnect_time_wait=9),
        max_reconnect_attempts=-1,
        reconnect_time_wait=2,
    )

    assert calls[0]["max_reconnect_attempts"] == 3
    assert calls[0]["reconnect_time_wait"] == 9


async def test_CONTROL_the_rest_of_the_pools_settings_are_unchanged(monkeypatch):
    calls = await _connect_kwargs(
        monkeypatch, PoolExtension(max_connections=1, ping_interval=44, max_outstanding_pings=2)
    )

    assert calls[0]["ping_interval"] == 44
    assert calls[0]["max_outstanding_pings"] == 2

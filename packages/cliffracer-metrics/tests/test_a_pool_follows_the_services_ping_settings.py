"""A pooled connection notices a silent partition as fast as its service does.

`ServiceConfig.ping_interval` and `max_outstanding_pings` set how soon nats-py notices a
connection that stopped answering without resetting its socket. The pool took its reconnect
policy from the service but not these two: it passed `ping_interval=120` and
`max_outstanding_pings=3` whatever the service said, so a service set to 5 s and 1 noticed a
partition on its own connection in 5 to 10 s and on its pooled ones in 360 to 480 s, and an
unconfigured service's pool was slower than nats-py's own default of two outstanding pings.
These tests read what the pool passes to the dial. An explicit value given to the extension wins.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from cliffracer_metrics import OptimizedNATSConnection, PoolExtension

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


async def _dial_kwargs(monkeypatch, pool: PoolExtension, **config) -> list[dict]:
    connect = AsyncMock(side_effect=lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    class Svc(CliffracerService):
        pooled = pool

    svc = Svc(ServiceConfig(name="svc", health_port=0, **config))
    await svc.container._setup_extensions()
    await svc.pooled.start()
    return [call.kwargs for call in connect.await_args_list]


async def test_the_pool_follows_the_services_configured_ping_settings(monkeypatch):
    calls = await _dial_kwargs(
        monkeypatch, PoolExtension(max_connections=2), ping_interval=5, max_outstanding_pings=1
    )

    assert len(calls) == 2
    for kwargs in calls:
        assert kwargs["ping_interval"] == 5
        assert kwargs["max_outstanding_pings"] == 1


async def test_a_service_that_sets_neither_leaves_natspys_defaults_in_force(monkeypatch):
    """Unset on the service passes nothing, as the service's own connection does."""
    calls = await _dial_kwargs(monkeypatch, PoolExtension(max_connections=1))

    assert "ping_interval" not in calls[0]
    assert "max_outstanding_pings" not in calls[0]


async def test_a_value_given_to_the_extension_replaces_the_services(monkeypatch):
    calls = await _dial_kwargs(
        monkeypatch,
        PoolExtension(max_connections=1, ping_interval=30, max_outstanding_pings=4),
        ping_interval=5,
        max_outstanding_pings=1,
    )

    assert calls[0]["ping_interval"] == 30
    assert calls[0]["max_outstanding_pings"] == 4


async def test_CONTROL_a_pool_built_directly_keeps_its_own_defaults(monkeypatch):
    connect = AsyncMock(side_effect=lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr("cliffracer.core.dial.connect", connect)
    pool = OptimizedNATSConnection(max_connections=1)

    await pool.connect()

    assert connect.await_args.kwargs["ping_interval"] == 120
    assert connect.await_args.kwargs["max_outstanding_pings"] == 3

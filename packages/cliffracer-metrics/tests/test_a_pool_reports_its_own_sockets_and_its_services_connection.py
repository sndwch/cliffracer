"""A pool reports its own sockets as `connected` and its service's connection as `service_connected`.

`is_connected` and `get_stats` answered from the owning service first, so with the service's own
connection down a pool whose sockets were up read `connected: False` and `active_connections: 0`:
the pool's state was hidden behind something adjacent. They now read the pool's sockets, and the
service's connection is its own key beside them.
"""

from unittest.mock import MagicMock

import pytest
from cliffracer_metrics import PoolExtension
from cliffracer_metrics.connection_pool import OptimizedNATSConnection

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


class Service:
    """The one property the pool reads from its owner."""

    def __init__(self, broker_connected: bool) -> None:
        self.is_broker_connected = broker_connected


def _pool(service, *, sockets: list[bool], max_connections: int = 4) -> OptimizedNATSConnection:
    pool = OptimizedNATSConnection(max_connections=max_connections, service=service)
    pool._connections = [MagicMock(is_connected=state) for state in sockets]
    return pool


def test_a_pool_whose_sockets_are_up_reads_connected_while_its_service_has_lost_the_broker():
    pool = _pool(Service(broker_connected=False), sockets=[True, True], max_connections=4)

    stats = pool.get_stats()

    assert pool.is_connected is True
    assert stats["active_connections"] == 2
    assert stats["utilization_percent"] == 50.0
    assert stats["service_connected"] is False
    assert pool.service_connected is False


def test_a_pool_of_dead_sockets_reads_down_while_its_service_has_the_broker():
    pool = _pool(Service(broker_connected=True), sockets=[False, False])

    stats = pool.get_stats()

    assert pool.is_connected is False
    assert stats["active_connections"] == 0
    assert stats["service_connected"] is True


def test_the_two_keys_move_independently():
    service = Service(broker_connected=True)
    pool = _pool(service, sockets=[True, False, True])

    before = pool.get_stats()
    service.is_broker_connected = False
    after = pool.get_stats()

    assert (before["active_connections"], before["service_connected"]) == (2, True)
    assert (after["active_connections"], after["service_connected"]) == (2, False)


@pytest.mark.parametrize("owner", [None, object()], ids=["no service", "service with no flag"])
def test_without_a_service_flag_the_sockets_decide_and_the_service_is_not_claimed(owner):
    up = _pool(owner, sockets=[False, True])
    down = _pool(owner, sockets=[False])

    assert up.is_connected is True
    assert up.get_stats()["active_connections"] == 1
    assert down.is_connected is False
    assert up.service_connected is None and up.get_stats()["service_connected"] is None


async def test_the_extension_reports_both_through_the_real_wiring():
    """`setup` gives the pool its service, and `/health` reads the pool."""

    class Svc(CliffracerService):
        pooled = PoolExtension(max_connections=2)

    svc = Svc(ServiceConfig(name="gated", health_port=0))
    await svc.container._setup_extensions()
    pool = svc.pooled.pool
    assert pool is not None and pool.service is svc
    pool._connections = [MagicMock(is_connected=True)]

    svc.nc = MagicMock(is_closed=False, is_connected=True, is_draining=False, is_connecting=False)
    assert svc.pooled.health_details() == {
        "connections": 1,
        "active_connections": 1,
        "closed_connections": 0,
        "connected": True,
        "service_connected": True,
    }

    svc.nc = None  # the service has lost the broker; the pool's socket still says connected
    assert svc.pooled.health_details() == {
        "connections": 1,
        "active_connections": 1,
        "closed_connections": 0,
        "connected": True,
        "service_connected": False,
    }
    assert pool.get_stats()["active_connections"] == 1

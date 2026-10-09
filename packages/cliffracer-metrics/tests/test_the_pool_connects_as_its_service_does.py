"""A pooled connection is the service's own connection, more than once.

The pool took the service's credentials and reconnect policy and nothing else the config says about
a connection: its replies came back on the default inbox prefix a permission-limited user cannot
subscribe to, every connection was anonymous on the broker, and a broker that did not answer made
the pool's start wait where the service's own start gives up at `connect_timeout`.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cliffracer_metrics import OptimizedNATSConnection, PoolExtension
from nats.errors import Error as NatsError

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


def _service(**config) -> CliffracerService:
    class Ingest(CliffracerService):
        pool = PoolExtension(max_connections=3)

    return Ingest(ServiceConfig(name="ingest", health_port=0, **config))


async def _pool_kwargs(svc: CliffracerService) -> list[dict]:
    with patch("cliffracer.core.dial.connect", new=AsyncMock()) as connect:
        await svc.container._setup_extensions()
        await svc.pool.start()
    return [call.kwargs for call in connect.call_args_list]


async def test_each_pooled_connection_uses_the_services_inbox_prefix():
    kwargs = await _pool_kwargs(_service(nats_inbox_prefix="_INBOX.ingest_workers"))

    assert len(kwargs) == 3
    assert {k["inbox_prefix"] for k in kwargs} == {"_INBOX.ingest_workers"}


async def test_with_no_inbox_prefix_configured_none_is_sent():
    kwargs = await _pool_kwargs(_service())

    assert all("inbox_prefix" not in k for k in kwargs)


async def test_each_pooled_connection_is_named_for_the_service_and_its_place_in_the_pool():
    kwargs = await _pool_kwargs(_service())

    assert [k["name"] for k in kwargs] == ["ingest-pool-1", "ingest-pool-2", "ingest-pool-3"]


async def test_the_pool_and_the_service_send_the_same_inbox_prefix_and_credentials():
    config = {
        "nats_inbox_prefix": "_INBOX.ingest_workers",
        "nats_user": "u",
        "nats_password": "p",
    }
    pooled = (await _pool_kwargs(_service(**config)))[0]

    svc = _service(**config)
    with patch("cliffracer.core.dial.connect", new=AsyncMock()) as connect:
        await svc.container.connection.connect()
    own = connect.call_args.kwargs

    expected = {"inbox_prefix": "_INBOX.ingest_workers", "user": "u", "password": "p"}
    assert {key: pooled[key] for key in expected} == expected
    assert {key: own[key] for key in expected} == expected


def _dialling_client(connect):
    """The client `cliffracer.core.dial` builds, whose `connect` is `connect`: the dial helper's own
    bound and cleanup run, and only nats-py's side of it is replaced."""
    return patch(
        "cliffracer.core.dial.nats.NATS",
        return_value=MagicMock(connect=connect, close=AsyncMock()),
    )


async def test_a_broker_that_does_not_answer_fails_the_pool_at_the_services_connect_timeout():
    async def never_answers(*args, **kwargs):
        await asyncio.sleep(30)

    svc = _service(connect_timeout=0.05)
    with _dialling_client(never_answers):
        await svc.container._setup_extensions()
        with pytest.raises(NatsError, match=r"no answer within connect_timeout=0\.05s"):
            await asyncio.wait_for(svc.pool.start(), timeout=5)


async def test_a_connect_timeout_of_none_puts_no_bound_on_a_connection():
    pool = OptimizedNATSConnection(max_connections=1, connect_timeout=None)

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.1)
        return AsyncMock()

    with _dialling_client(slow):
        await pool.connect()

    assert len(pool._connections) == 1


async def test_a_pool_built_without_a_name_connects_without_one():
    pool = OptimizedNATSConnection(max_connections=1)
    with patch("cliffracer.core.dial.connect", new=AsyncMock()) as connect:
        await pool.connect()

    assert "name" not in connect.call_args.kwargs

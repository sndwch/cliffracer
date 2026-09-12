"""Tests for PoolExtension connection management and lifecycle."""

import os
import socket

import pytest
from cliffracer_metrics import PoolExtension

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


BROKER_ENV = "CLIFFRACER_TEST_NATS_URL"


def _broker_url() -> str:
    """The address the operator pointed the suite at, or the library default.

    Read here rather than inherited: see the module docstring. `ServiceConfig`'s
    default is the fallback so this says the same thing as an unconfigured run.
    """
    return os.getenv(BROKER_ENV) or ServiceConfig.model_fields["nats_url"].default


def _endpoint(url: str) -> tuple[str, int]:
    rest = url.split("://", 1)[-1]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    host, _, port = rest.partition(":")
    return host or "localhost", int(port or 4222)


@pytest.fixture
def broker() -> str:
    """The broker URL, or an outcome that is not a silent hang.

    One TCP connect, which answers the only question that matters here -- will
    a connect block. It does not prove NATS is healthy.

    ASKED-FOR AND ABSENT IS A FAILURE, not a skip: someone set the variable, so
    a green run that quietly ran none of these tests would be a lie. Nothing
    asked for and nothing there is a skip, which is what the `tests/` tree does
    for a developer with no broker at all.
    """
    url = _broker_url()
    host, port = _endpoint(url)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect((host, port))
    except OSError as exc:
        if os.getenv(BROKER_ENV):
            pytest.fail(f"${BROKER_ENV} is {url}, which is not accepting connections: {exc}")
        pytest.skip(f"no broker at {url}; set ${BROKER_ENV} to point these at one")
    finally:
        sock.close()
    return url


def _svc(config: ServiceConfig | None = None):
    class Pooled(CliffracerService):
        pool = PoolExtension(max_connections=3)

    return Pooled(config or ServiceConfig(name="pooled", health_port=0))


# --- credentials reach the pool ---------------------------------------------


async def test_user_and_password_reach_the_pool():
    svc = _svc(ServiceConfig(name="p", health_port=0, nats_user="svc", nats_password="s3cret"))
    await svc.container._setup_extensions()
    assert svc.pool.pool.auth_kwargs == {"user": "svc", "password": "s3cret"}


async def test_a_credentials_file_reaches_the_pool():
    svc = _svc(ServiceConfig(name="p", health_port=0, nats_credentials_file="/tmp/x.creds"))
    await svc.container._setup_extensions()
    assert svc.pool.pool.auth_kwargs == {"user_credentials": "/tmp/x.creds"}


async def test_no_credentials_forwards_nothing():
    """The other half. Without this, a pool that forwarded `{}` for every
    config would pass both tests above by never forwarding anything at all."""
    svc = _svc()
    await svc.container._setup_extensions()
    assert svc.pool.pool.auth_kwargs == {}


async def test_the_pool_takes_the_service_url():
    svc = _svc(ServiceConfig(name="p", health_port=0, nats_url="nats://broker:4222"))
    await svc.container._setup_extensions()
    assert svc.pool.pool.nats_url == "nats://broker:4222"


# --- lifecycle ---------------------------------------------------------------


async def test_the_pool_is_built_in_setup_not_before():
    svc = _svc()
    assert svc.pool.pool is None
    await svc.container._setup_extensions()
    assert svc.pool.pool is not None


async def test_two_services_do_not_share_a_pool():
    """`bind()` is a shallow copy: a pool built in `__init__` would be one
    object shared by every service declaring the extension."""
    a, b = _svc(), _svc()
    await a.container._setup_extensions()
    await b.container._setup_extensions()
    assert a.pool.pool is not b.pool.pool


async def test_stop_is_safe_when_start_never_ran():
    """`stop()` runs on a service that failed before `start()`."""
    svc = _svc()
    await svc.container._setup_extensions()
    await svc.pool.stop()  # must not raise


async def test_health_details_before_setup_contributes_nothing():
    ext = PoolExtension()
    assert ext.health_details() is None


async def test_health_details_reports_the_pool():
    svc = _svc()
    await svc.container._setup_extensions()
    assert svc.pool.health_details() == {"connections": 0, "connected": False}


# --- a service WITHOUT the extension is unchanged -----------------------------


async def test_a_service_without_the_extension_has_no_pool():
    svc = CliffracerService(ServiceConfig(name="plain", health_port=0))
    await svc.container._setup_extensions()
    assert not hasattr(svc, "pool")
    body = await svc.health_check()
    assert "pool" not in body


# --- the pool is NOT the service's connection ---------------------------------


@pytest.mark.nats_required
async def test_the_pools_connections_are_not_the_services_connection(broker):
    """Identity, not behaviour: option (a) is that these are separate.

    If a later change routed core through the pool, this is what would notice.
    """
    svc = _svc(ServiceConfig(name="p", health_port=0, nats_url=broker))
    await svc.start()
    try:
        pooled = await svc.pool.get_connection()
        assert pooled is not svc.nc
        assert all(conn is not svc.nc for conn in svc.pool.pool._connections)
    finally:
        await svc.stop()


@pytest.mark.nats_required
async def test_a_handler_can_answer_through_the_pool(broker):
    """A real round trip: the pool is connected and usable from a handler."""

    class Echo(CliffracerService):
        @rpc
        async def ping(self) -> dict[str, bool]:
            return {"pong": True}

    class Caller(CliffracerService):
        pool = PoolExtension(max_connections=2)

    echo = Echo(ServiceConfig(name="echo_pool", health_port=0, nats_url=broker))
    caller = Caller(ServiceConfig(name="caller_pool", health_port=0, nats_url=broker))
    await echo.start()
    await caller.start()
    try:
        reply = await caller.pool.request("echo_pool.rpc.ping", b"{}", timeout=5.0)
        assert b"pong" in reply.data
    finally:
        await caller.stop()
        await echo.stop()


# --- Send-side hook isolation ---------------------------------------------


@pytest.mark.nats_required
async def test_send_side_hooks_do_not_fire_for_a_pool_request(broker):
    """Verify send-side hooks wrap service send methods and do not intercept pool requests."""
    seen: list[str] = []

    class Watcher(Extension):
        async def before_call(self, ctx):
            seen.append(ctx.kind)

    class Echo(CliffracerService):
        @rpc
        async def ping(self) -> dict[str, bool]:
            return {"pong": True}

    class Caller(CliffracerService):
        watcher = Watcher()
        pool = PoolExtension(max_connections=2)

    echo = Echo(ServiceConfig(name="echo_hooks", health_port=0, nats_url=broker))
    caller = Caller(ServiceConfig(name="caller_hooks", health_port=0, nats_url=broker))
    await echo.start()
    await caller.start()
    try:
        await caller.pool.request("echo_hooks.rpc.ping", b"{}", timeout=5.0)
        assert seen == [], f"a pool request fired send-side hooks: {seen}"

        await caller.call_rpc("echo_hooks", "ping")
        assert seen == ["call_rpc"], seen
    finally:
        await caller.stop()
        await echo.stop()

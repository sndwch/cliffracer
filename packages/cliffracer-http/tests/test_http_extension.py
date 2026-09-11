"""HttpExtension: routes, websockets, health on the app, per-instance state."""

import asyncio
import json

import pytest
from cliffracer_http import HttpExtension
from fastapi.testclient import TestClient

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener


class Svc(CliffracerService):
    http = HttpExtension(port=0)

    @http.get("/hello/{name}")
    async def hello(self, name: str) -> dict:
        return {"hello": name}

    @http.websocket("/ws/echo")
    async def echo(self, websocket):
        await websocket.accept()
        text = await websocket.receive_text()
        await websocket.send_text(text.upper())


async def _setup(svc):
    await svc.container._setup_extensions()
    svc._discover_handlers()
    return TestClient(svc.http.app)


async def _core_listener_get(port: int, path: str) -> tuple[int, dict]:
    """Read the core listener over a socket, the way a probe does.

    It speaks HTTP on stdlib asyncio rather than through an app, so there is
    no TestClient for it; this is the same eight lines tests/unit/
    test_health_listener.py uses. Duplicated rather than imported: this
    distribution's tests must run against an installed cliffracer without the
    repo's own `tests` package on the path.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), (json.loads(body) if body else {})


def _connect(svc) -> None:
    """The repo's idiom for a service that is up and connected, no broker."""
    svc.container.lifecycle._running = True
    svc.nc = type("C", (), {"is_closed": False, "is_connected": True})()


@pytest.mark.unit
async def test_routes_are_registered_from_markers():
    client = await _setup(Svc(ServiceConfig(name="s")))
    assert client.get("/hello/bob").json() == {"hello": "bob"}


@pytest.mark.unit
async def test_health_and_info_are_served_on_the_app_too():
    client = await _setup(Svc(ServiceConfig(name="s")))
    assert client.get("/health").json()["service"] == "s"
    assert client.get("/info").json()["name"] == "s"


@pytest.mark.unit
async def test_health_answers_503_when_the_service_is_not_healthy():
    """Verify /health returns HTTP 503 when the service is stopped or unhealthy."""
    svc = Svc(ServiceConfig(name="s"))
    client = await _setup(svc)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "stopped"


@pytest.mark.unit
async def test_health_still_answers_200_when_the_service_is_healthy():
    """Verify /health returns HTTP 200 when the service is connected and healthy."""
    svc = Svc(ServiceConfig(name="s"))
    client = await _setup(svc)
    _connect(svc)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.unit
@pytest.mark.parametrize("connected, expected", [(True, 200), (False, 503)])
async def test_the_two_health_endpoints_answer_a_service_identically(connected, expected):
    """Verify both health endpoints return matching status codes for identical service states."""
    svc = Svc(ServiceConfig(name="s"))
    client = await _setup(svc)
    if connected:
        _connect(svc)

    # Constructed here rather than svc.health_listener.start(): that one binds
    # config.health_port, which defaults to 8000 and would fail open on a busy
    # port -- a listener that never started answers nothing and this test
    # would pass on one endpoint.
    listener = HealthListener(svc, "127.0.0.1", 0)
    await listener.start()
    try:
        core_code, core_body = await _core_listener_get(listener.port, "/health")
    finally:
        await listener.stop()
    route = client.get("/health")

    assert (route.status_code, core_code) == (expected, expected)
    assert route.json()["status"] == core_body["status"]


@pytest.mark.unit
async def test_websocket_handler_is_registered():
    client = await _setup(Svc(ServiceConfig(name="s")))
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "HI"


@pytest.mark.unit
async def test_health_contribution_reports_websockets_under_the_extension_name():
    svc = Svc(ServiceConfig(name="s"))
    await _setup(svc)
    health = await svc.health_check()
    assert health["http"] == {"websockets": {"active_connections": 0, "registered_handlers": 1}}


@pytest.mark.unit
async def test_two_services_do_not_share_websocket_state():
    """Verify per-instance state isolation across multiple service instances."""
    a, b = Svc(ServiceConfig(name="a")), Svc(ServiceConfig(name="b"))
    await _setup(a)
    await _setup(b)

    assert a.http.app is not b.http.app
    assert a.http.active_connections is not b.http.active_connections
    assert a.http._websocket_handlers is not b.http._websocket_handlers

    a.http.active_connections.add(object())
    assert len(b.http.active_connections) == 0


@pytest.mark.unit
async def test_the_extension_is_bound_per_instance_and_the_class_attribute_stays_unbound():
    a = Svc(ServiceConfig(name="a"))
    assert a.http is not Svc.http
    assert a.http.service is a
    assert Svc.http.service is None


@pytest.mark.unit
async def test_the_extension_takes_the_core_listener_off_the_same_port():
    svc = Svc(ServiceConfig(name="s", health_port=0))
    await svc.container._setup_extensions()
    # Assert the exact type to ensure we imported HealthListener correctly.
    assert isinstance(svc.health_listener, HealthListener)
    assert svc.health_listener._disabled is not None


@pytest.mark.unit
async def test_the_extension_takes_the_core_listener_off_a_DIFFERENT_port_too():
    """Verify core health listener is disabled even when HttpExtension uses a different port."""

    class S(CliffracerService):
        http = HttpExtension(port=9)

    svc = S(ServiceConfig(name="s", health_port=0))
    await svc.container._setup_extensions()
    await svc.health_listener.start()
    try:
        assert svc.health_listener._server is None, (
            "the core listener bound a socket while cliffracer-http serves /health on its own port"
        )
        assert svc.health_listener.port is None
    finally:
        await svc.health_listener.stop()


@pytest.mark.unit
async def test_the_reason_names_the_port_the_extension_serves_on():
    """The disable reason is what an operator reads in the log to find /health."""

    class S(CliffracerService):
        http = HttpExtension(port=9)

    svc = S(ServiceConfig(name="s", health_port=0))
    await svc.container._setup_extensions()
    reason = svc.health_listener._disabled
    # Named first. `"9" in None` is a TypeError, which reports as an error
    # about str and NoneType rather than as "the listener was never disabled".
    assert reason is not None, "the core listener was not disabled at all"
    assert "9" in reason
    assert "cliffracer_http" in reason


@pytest.mark.unit
async def test_info_details_lists_the_websocket_handlers():
    svc = Svc(ServiceConfig(name="s"))
    await _setup(svc)
    assert svc.get_service_info()["http"] == {"websocket_handlers": ["/ws/echo"]}


@pytest.mark.unit
async def test_broadcast_reaches_every_live_socket_and_drops_the_dead_ones():
    """`broadcast_to_websockets` is in the extension's interface and the plan
    ships no test for it. A send that raises must not stop the others, and the
    dead socket must be dropped rather than retried forever."""

    class Sock:
        def __init__(self, dead=False):
            self.dead, self.sent = dead, []

        async def send_text(self, text):
            if self.dead:
                raise RuntimeError("closed")
            self.sent.append(text)

    svc = Svc(ServiceConfig(name="s"))
    await _setup(svc)
    live_a, dead, live_b = Sock(), Sock(dead=True), Sock()
    svc.http.active_connections.update({live_a, dead, live_b})

    await svc.http.broadcast_to_websockets({"hello": "all"})

    assert live_a.sent == ['{"hello": "all"}']
    assert live_b.sent == ['{"hello": "all"}'], "a raising socket stopped the others"
    assert dead not in svc.http.active_connections
    assert {live_a, live_b} <= svc.http.active_connections


@pytest.mark.unit
async def test_broadcast_survives_concurrent_client_disconnect():
    """Verify broadcast_to_websockets handles concurrent disconnects during iteration."""

    class SuspendingSock:
        def __init__(self, name: str, hook=None):
            self.name = name
            self.sent = []
            self.hook = hook

        async def send_text(self, text: str):
            if self.hook:
                await self.hook()
            else:
                await asyncio.sleep(0)
            self.sent.append(text)

    svc = Svc(ServiceConfig(name="s"))
    await _setup(svc)

    socks = [SuspendingSock(f"sock_{i}") for i in range(5)]
    svc.http.active_connections.update(socks)

    # When sock_0 sends, simulate concurrent disconnection of sock_2 and sock_4
    async def mid_broadcast_disconnect():
        await asyncio.sleep(0)
        svc.http.active_connections.discard(socks[2])
        svc.http.active_connections.discard(socks[4])

    socks[0].hook = mid_broadcast_disconnect

    await svc.http.broadcast_to_websockets({"event": "ping"})

    # Every socket in the initial snapshot must receive the message
    for sock in socks:
        assert sock.sent == ['{"event": "ping"}'], f"{sock.name} was silently skipped"

    # Disconnected sockets were removed from active_connections
    assert socks[2] not in svc.http.active_connections
    assert socks[4] not in svc.http.active_connections
    assert {socks[0], socks[1], socks[3]} <= svc.http.active_connections


@pytest.mark.unit
async def test_broadcast_concurrent_disconnect_with_dead_socket():
    """When a client disconnects mid-broadcast and raises on send_text, the error
    is caught, the socket is dropped, and the remaining sockets still receive the message."""

    class Sock:
        def __init__(self, name: str, hook=None, dead=False):
            self.name = name
            self.sent = []
            self.hook = hook
            self.dead = dead

        async def send_text(self, text: str):
            if self.hook:
                await self.hook()
            if self.dead:
                raise RuntimeError("socket closed")
            self.sent.append(text)

    svc = Svc(ServiceConfig(name="s"))
    await _setup(svc)

    s0, s1, s2 = Sock("s0"), Sock("s1", dead=True), Sock("s2")
    svc.http.active_connections.update({s0, s1, s2})

    async def disconnect_s2():
        await asyncio.sleep(0)
        svc.http.active_connections.discard(s2)

    s0.hook = disconnect_s2

    await svc.http.broadcast_to_websockets({"event": "update"})

    assert s0.sent == ['{"event": "update"}']
    assert s2.sent == ['{"event": "update"}']
    assert s1 not in svc.http.active_connections
    assert s2 not in svc.http.active_connections
    assert s0 in svc.http.active_connections


@pytest.mark.unit
def test_config_reads_its_own_env_prefix(monkeypatch):
    """Spec 3.5: each extension's settings carry its own prefix, so installing
    the extension is the only thing that adds them."""
    from cliffracer_http import HttpConfig

    monkeypatch.setenv("CLIFFRACER_HTTP_PORT", "9123")
    monkeypatch.setenv("CLIFFRACER_HTTP_HOST", "127.0.0.5")
    cfg = HttpConfig()
    assert (cfg.host, cfg.port) == ("127.0.0.5", 9123)
    assert HttpExtension().port == 9123, "constructor default must fall back to env"
    assert HttpExtension(port=1).port == 1, "an explicit argument must win over env"


# HTTP endpoint and websocket route decorator tests.


class Decorated(CliffracerService):
    http = HttpExtension(port=0)

    @http.get("/typed/{n}", response_model=None, tags=["ported"])
    async def typed(self, n: str) -> dict:
        return {"n": n}

    async def not_a_route(self) -> dict:
        return {}

    @http.websocket("/ws/count")
    async def counter(self, websocket):
        await websocket.accept()
        await websocket.send_text("1")


@pytest.mark.unit
async def test_routes_are_registered_before_start_not_at_first_request():
    """Registration happens at discovery, so a route exists before the server
    is ever started. Otherwise a service could pass its own health check and
    404 the first real request."""
    svc = Decorated(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    paths = {r.path for r in svc.http.app.routes if hasattr(r, "path")}
    assert "/typed/{n}" in paths


@pytest.mark.unit
async def test_an_undecorated_method_is_not_a_route():
    svc = Decorated(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    paths = {r.path for r in svc.http.app.routes if hasattr(r, "path")}
    assert not any("not_a_route" in p for p in paths)


@pytest.mark.unit
async def test_decorator_kwargs_reach_fastapi():
    """`@http.get(path, **kw)` must forward kw to add_api_route; without this
    the decorator silently accepts options it then drops."""
    svc = Decorated(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    route = next(r for r in svc.http.app.routes if getattr(r, "path", None) == "/typed/{n}")
    assert "ported" in route.tags


@pytest.mark.unit
async def test_the_built_in_health_and_info_routes_survive_user_routes():
    svc = Decorated(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    paths = {r.path for r in svc.http.app.routes if hasattr(r, "path")}
    assert {"/health", "/info", "/typed/{n}"} <= paths


@pytest.mark.unit
async def test_a_service_without_the_extension_is_unaffected():
    """The counterpart of core's `test_a_service_without_the_http_mixin_is_
    unaffected`: declaring no extension must leave a service with no app and no
    HTTP behaviour, rather than a half-configured one."""

    class Plain(CliffracerService):
        pass

    svc = Plain(ServiceConfig(name="plain"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    assert not hasattr(svc, "http")


@pytest.mark.unit
async def test_a_disconnecting_socket_is_removed_from_active_connections():
    """Ported from core's websocket connection-management case: the set must not
    grow without bound as clients come and go."""
    svc = Decorated(ServiceConfig(name="s"))
    await _setup(svc)
    client = TestClient(svc.http.app)

    assert len(svc.http.active_connections) == 0
    with client.websocket_connect("/ws/count") as ws:
        assert ws.receive_text() == "1"
    assert len(svc.http.active_connections) == 0, "the socket was not discarded on disconnect"


@pytest.mark.unit
async def test_a_caller_supplied_title_reaches_the_app():
    """Verify custom title passed to HttpExtension is applied to FastAPI instance."""

    class Svc(CliffracerService):
        http = HttpExtension(port=0, title="Orders API v2")

    svc = Svc(ServiceConfig(name="orders"))
    await svc.container._setup_extensions()

    assert svc.http.app.title == "Orders API v2"


@pytest.mark.unit
async def test_without_a_title_the_service_name_is_still_the_default():
    """Verify default title uses service name when no title is explicitly passed."""

    class Svc(CliffracerService):
        http = HttpExtension(port=0)

    svc = Svc(ServiceConfig(name="orders"))
    await svc.container._setup_extensions()

    assert svc.http.app.title == "orders API"


@pytest.mark.unit
async def test_other_fastapi_kwargs_still_pass_through():
    """Verify additional FastAPI kwargs pass through to the FastAPI instance."""

    class Svc(CliffracerService):
        http = HttpExtension(port=0, description="orders service", version="9.9.9")

    svc = Svc(ServiceConfig(name="orders"))
    await svc.container._setup_extensions()

    assert svc.http.app.description == "orders service"
    assert svc.http.app.version == "9.9.9"


@pytest.mark.unit
async def test_two_services_do_not_share_a_default_title():
    """Verify multiple services sharing a class attribute do not overwrite each other's default title."""

    class Svc(CliffracerService):
        http = HttpExtension(port=0)

    a = Svc(ServiceConfig(name="alpha"))
    b = Svc(ServiceConfig(name="beta"))
    await a.container._setup_extensions()
    await b.container._setup_extensions()

    assert a.http.app.title == "alpha API"
    assert b.http.app.title == "beta API", (
        "beta inherited alpha's default title: the default was written into the "
        "shared _fastapi_kwargs rather than into a per-setup copy"
    )


@pytest.mark.unit
def test_the_shared_kwargs_cannot_be_written_to_at_all():
    """Verify _fastapi_kwargs is immutable to prevent cross-service configuration leaks."""
    extension = HttpExtension(port=0, description="x")

    with pytest.raises(AttributeError):
        extension._fastapi_kwargs.setdefault("title", "leaked")
    with pytest.raises(TypeError):
        extension._fastapi_kwargs["title"] = "leaked"
    with pytest.raises(AttributeError):
        extension._fastapi_kwargs.update({"title": "leaked"})

    # And reading is untouched, which is what setup() does with it: a mapping
    # that refused writes by refusing everything would break the merge instead.
    assert {**extension._fastapi_kwargs} == {"description": "x"}
    assert {**extension._fastapi_kwargs, "title": "t"} == {"description": "x", "title": "t"}

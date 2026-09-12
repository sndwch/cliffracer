"""Tests for dependency health verification."""

import asyncio
import dataclasses
import socket
import time

import pytest

from cliffracer import Dependency, dependency
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit


class _FakeNats:
    """Stands in for nats-py's client. Only `is_closed` is read here."""

    def __init__(self, is_closed=False, is_connected=True):
        self.is_closed = is_closed
        self.is_connected = is_connected


def _running_service(cls=CliffracerService, name="dep-test"):
    svc = cls(ServiceConfig(name=name))
    svc._running = True
    svc.nc = _FakeNats(is_closed=False, is_connected=True)
    return svc


# --- real endpoints --------------------------------------------------------


class Listener:
    """TCP server test fixture. `answer=False` accepts and remains silent."""

    def __init__(self, answer=True):
        self.answer = answer
        self.server = None
        self.port = None
        self._handlers = []

    async def __aenter__(self):
        async def handle(reader, writer):
            # Wait until cancellation rather than using a fixed sleep duration to allow clean teardown.
            self._handlers.append(asyncio.current_task())
            if self.answer:
                writer.write(b"ok\n")
                await writer.drain()
            else:
                await asyncio.Event().wait()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        for task in self._handlers:
            task.cancel()
        self.server.close()
        await self.server.wait_closed()

    def probe(self, read=True):
        async def check():
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            try:
                if read:
                    data = await reader.readline()
                    if not data:
                        raise ConnectionError("dependency closed without answering")
            finally:
                writer.close()

        return check


def closed_port():
    """A port number with nothing listening: bind, read the port, release it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    async def check():
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()

    return check


# --- the states ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reachable_dependency_leaves_the_service_healthy():
    async with Listener() as listener:
        svc = _running_service()
        svc.add_dependency("probe-target", listener.probe(), timeout=2.0)
        health = await svc.health_check()

    assert health["status"] == "healthy", health
    assert health["dependencies"]["probe-target"]["ok"] is True
    assert health["dependencies"]["probe-target"]["error"] is None
    assert "unhealthy_dependencies" not in health


@pytest.mark.asyncio
async def test_an_unreachable_dependency_makes_the_service_unhealthy():
    """Verify an unreachable dependency sets service status to unhealthy."""
    svc = _running_service()
    svc.add_dependency("postgres", closed_port(), timeout=2.0)

    health = await svc.health_check()

    assert health["status"] == "unhealthy", health
    assert health["unhealthy_dependencies"] == ["postgres"]
    assert health["dependencies"]["postgres"]["ok"] is False
    # The error names the refusal rather than being a bare False.
    assert health["dependencies"]["postgres"]["error"]
    # Liveness and the broker are unchanged and still readable.
    assert health["nats_connected"] is True


@pytest.mark.asyncio
async def test_ANY_failing_dependency_changes_status_and_is_named():
    """Verify any failing dependency marks status unhealthy and appears in unhealthy_dependencies."""
    svc = _running_service()
    svc.add_dependency("metrics", closed_port(), timeout=2.0)

    health = await svc.health_check()

    assert health["status"] == "unhealthy", health
    assert health["unhealthy_dependencies"] == ["metrics"], health
    assert health["dependencies"]["metrics"]["ok"] is False


@pytest.mark.asyncio
async def test_the_keyword_is_gone_rather_than_ignored():
    """Verify required= keyword argument is rejected with TypeError."""
    svc = _running_service()
    with pytest.raises(TypeError):
        svc.add_dependency("metrics", closed_port(), required=False, timeout=2.0)
    with pytest.raises(TypeError):
        dependency("metrics", required=True)


@pytest.mark.asyncio
async def test_a_hung_dependency_times_out_instead_of_hanging_the_endpoint():
    async with Listener(answer=False) as listener:
        svc = _running_service()
        svc.add_dependency("slow", listener.probe(), timeout=0.2)

        started = time.monotonic()
        # Bound health_check execution with timeout.
        health = await asyncio.wait_for(svc.health_check(), timeout=5)
        elapsed = time.monotonic() - started

    assert health["status"] == "unhealthy", health
    assert "timed out after 0.2s" in health["dependencies"]["slow"]["error"]
    # The listener sleeps 30s. Anything near that means the bound did nothing.
    assert elapsed < 5, elapsed


@pytest.mark.asyncio
async def test_a_probe_that_raises_is_a_failed_dependency_not_a_dead_endpoint():
    async def broken():
        raise RuntimeError("probe itself is wrong")

    svc = _running_service()
    svc.add_dependency("broken", broken)

    health = await svc.health_check()

    assert health["status"] == "unhealthy", health
    assert "RuntimeError: probe itself is wrong" in health["dependencies"]["broken"]["error"]
    # Everything else still answered.
    assert health["service"] == "dep-test"
    assert "features" in health


@pytest.mark.asyncio
async def test_stopped_and_disconnected_still_win_over_unhealthy():
    """A more specific diagnosis must not be replaced by a vaguer one."""
    svc = _running_service()
    svc.add_dependency("postgres", closed_port())

    svc._running = False
    assert (await svc.health_check())["status"] == "stopped"

    svc._running = True
    svc.nc = _FakeNats(is_closed=True)
    assert (await svc.health_check())["status"] == "disconnected"


@pytest.mark.asyncio
async def test_a_service_with_no_dependencies_is_unchanged():
    """The payload must not grow a key for services that declare nothing."""
    svc = _running_service()
    health = await svc.health_check()

    assert health["status"] == "healthy"
    assert "dependencies" not in health
    assert "unhealthy_dependencies" not in health


# --- the properties that decide whether this helps or hurts ----------------


@pytest.mark.asyncio
async def test_probes_run_concurrently_not_serially():
    """Five 0.3s timeouts run serially are a 1.5s health endpoint."""
    async with Listener(answer=False) as listener:
        svc = _running_service()
        for i in range(5):
            svc.add_dependency(f"slow-{i}", listener.probe(), timeout=0.3)

        started = time.monotonic()
        # Bound execution timeout.
        health = await asyncio.wait_for(svc.health_check(), timeout=5)
        elapsed = time.monotonic() - started

    assert len(health["dependencies"]) == 5
    assert all(not d["ok"] for d in health["dependencies"].values())
    # Serial would be >= 1.5s; concurrent is one timeout plus overhead.
    assert elapsed < 1.0, elapsed


@pytest.mark.asyncio
async def test_every_dependency_is_reported_even_when_one_fails():
    """Verify all dependencies are reported in health details even when one fails."""
    async with Listener() as listener:
        svc = _running_service()
        svc.add_dependency("good", listener.probe())
        svc.add_dependency("bad", closed_port())

        health = await svc.health_check()

    assert health["dependencies"]["good"]["ok"] is True
    assert health["dependencies"]["bad"]["ok"] is False
    assert health["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_latency_is_measured_and_plausible():
    """Verify latency is recorded as float and reflects timeout duration."""
    async with Listener(answer=False) as listener:
        svc = _running_service()
        svc.add_dependency("slow", listener.probe(), timeout=0.2)
        health = await asyncio.wait_for(svc.health_check(), timeout=5)

    latency = health["dependencies"]["slow"]["latency_ms"]
    assert isinstance(latency, float)
    assert latency >= 150.0, f"a 0.2s timeout should measure ~200ms, got {latency}"
    assert latency < 2000.0, latency

    # And the fast case still reports a real number rather than None.
    async with Listener() as listener:
        svc = _running_service()
        svc.add_dependency("target", listener.probe())
        health = await svc.health_check()

    assert isinstance(health["dependencies"]["target"]["latency_ms"], float)


@pytest.mark.asyncio
async def test_detail_reaches_the_payload():
    """An operator reading a failure needs to know WHICH postgres."""
    svc = _running_service()
    svc.add_dependency("postgres", closed_port(), database="jorbo", host="terry")

    health = await svc.health_check()

    assert health["dependencies"]["postgres"]["database"] == "jorbo"
    assert health["dependencies"]["postgres"]["host"] == "terry"


# --- the machinery's own failure -------------------------------------------


@pytest.mark.asyncio
async def test_a_broken_dependency_check_is_reported_and_makes_the_service_unhealthy():
    """Verify internal failure in dependency check machinery marks service unhealthy."""
    svc = _running_service()

    # A dependency list that is not a list of Dependency objects: exactly the
    # "probe attribute is not callable" shape the guard exists for. Set
    # directly rather than through add_dependency, which would only ever
    # produce well-formed entries -- there is no supported way to reach this
    # branch, which is why it went untested and unnoticed.
    svc._dependencies = ["not a dependency"]

    health = await svc.health_check()

    assert health["status"] == "unhealthy", health
    assert "dependencies_error" in health, health
    assert health["dependencies_error"], health
    # And it must NOT look like a service with no dependencies.
    assert health.get("dependencies") in (None, {}), health


@pytest.mark.asyncio
async def test_the_machinery_failure_does_not_take_the_endpoint_down():
    """It still answers, with everything that does not depend on the checks."""
    svc = _running_service()
    svc._dependencies = ["not a dependency"]

    health = await svc.health_check()

    assert health["service"] == "dep-test"
    assert "features" in health
    assert health["nats_connected"] is True


# --- registration ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_decorator_registers_a_dependency():
    class Api(CliffracerService):
        @dependency("postgres", timeout=0.5, database="jorbo")
        async def _check_db(self):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            writer.close()

    svc = _running_service(Api)
    svc.port = _free_port()
    health = await svc.health_check()

    assert health["status"] == "unhealthy"
    assert health["dependencies"]["postgres"]["database"] == "jorbo"
    # Verify exact keys in dependency status dictionary.
    assert set(health["dependencies"]["postgres"]) == {
        "database",
        "ok",
        "error",
        "latency_ms",
    }, health["dependencies"]["postgres"]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_re_registering_a_name_replaces_it():
    """Two checks under one name would report one and silently drop the
    other, and which survived would depend on ordering."""
    async with Listener() as listener:
        svc = _running_service()
        svc.add_dependency("db", closed_port())
        svc.add_dependency("db", listener.probe())

        health = await svc.health_check()

    assert len(svc._dependencies) == 1
    assert health["dependencies"]["db"]["ok"] is True
    assert health["status"] == "healthy"


@pytest.mark.asyncio
async def test_dependencies_are_reported_in_a_stable_order():
    async with Listener() as listener:
        svc = _running_service()
        for name in ("zebra", "alpha", "middle"):
            svc.add_dependency(name, listener.probe())
        health = await svc.health_check()

    assert list(health["dependencies"]) == ["alpha", "middle", "zebra"]


def test_a_dependency_is_immutable_once_declared():
    dep = Dependency(name="x", probe=lambda: None)
    # Verify `name` is actually a dataclass field so we get the
    # FrozenInstanceError expected below.
    assert "name" in {f.name for f in dataclasses.fields(Dependency)}
    assert dep.name == "x"
    #
    # IMMUTABILITY: FrozenInstanceError specifically, not Exception. The blind
    # form also passes on AttributeError or TypeError, so it would stay green
    # if the field became a read-only property or a slot -- neither of which
    # is a frozen dataclass.
    with pytest.raises(dataclasses.FrozenInstanceError):
        dep.name = "y"


# --- one name, one probe ---------------------------------------------------


def test_one_name_on_two_methods_is_refused_at_registration():
    """Verify duplicate dependency names on distinct methods raise ConfigurationError."""

    class Api(CliffracerService):
        @dependency("postgres")
        async def _check_db(self):
            pass

        @dependency("postgres")
        async def _check_db_replica(self):
            pass

    with pytest.raises(ConfigurationError) as caught:
        Api(ServiceConfig(name="s"))

    message = str(caught.value)
    assert "postgres" in message
    assert "Api._check_db" in message and "Api._check_db_replica" in message, message


async def test_a_subclass_may_override_a_base_class_dependency():
    """Verify subclass dependency overrides base class dependency."""
    ran = []

    class Base(CliffracerService):
        @dependency("postgres")
        async def _check_db_zz(self):
            ran.append("base")

    class Sub(Base):
        @dependency("postgres")
        async def _check_db_aa(self):
            ran.append("subclass")

    svc = _running_service(Sub)
    assert [dep.name for dep in svc._dependencies] == ["postgres"]

    await svc.health_check()
    assert ran == ["subclass"], f"the base's probe ran instead: {ran}"


def test_CONTROL_one_name_on_one_method_still_registers():
    """A rule that refused everything would satisfy the test above and take
    every declared dependency in the framework with it."""

    class Api(CliffracerService):
        @dependency("postgres", database="jorbo")
        async def _check_db(self):
            pass

        @dependency("s3", bucket="uploads")
        async def _check_s3(self):
            pass

    svc = Api(ServiceConfig(name="s"))
    assert sorted(dep.name for dep in svc._dependencies) == ["postgres", "s3"]


def test_CONTROL_add_dependency_still_replaces_by_name():
    """The runtime path is documented to REPLACE, and this change does not
    touch it: the refusal is about two declarations in a class body, where
    neither can be meant as a replacement for the other."""
    svc = _running_service()
    svc.add_dependency("metrics", closed_port(), timeout=2.0)
    svc.add_dependency("metrics", closed_port(), timeout=2.0)
    assert [dep.name for dep in svc._dependencies] == ["metrics"]


def test_two_mixins_claiming_one_name_are_refused():
    """Verify sibling classes defining identical dependency names raise ConfigurationError."""

    class Storage:
        @dependency("postgres")
        async def _check_storage(self):
            pass

    class Reporting:
        @dependency("postgres")
        async def _check_reporting(self):
            pass

    class Api(Storage, Reporting, CliffracerService):
        pass

    with pytest.raises(ConfigurationError) as caught:
        Api(ServiceConfig(name="s"))

    message = str(caught.value)
    assert "Storage._check_storage" in message and "Reporting._check_reporting" in message, message

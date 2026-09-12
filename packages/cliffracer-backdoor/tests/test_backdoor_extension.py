"""Tests for BackdoorExtension and CLI."""

import asyncio
import socket

import pytest
from cliffracer_backdoor import (
    BackdoorConfig,
    BackdoorExtension,
    NATSInspector,
    ServiceInspector,
)
from cliffracer_backdoor.cli import main, parse_endpoint

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


def test_the_console_is_disabled_unless_asked_for():
    """A debug console evaluates arbitrary Python in the service process, so
    default-on would be a remote code execution endpoint rather than a feature.
    """
    assert BackdoorConfig().enabled is False


def test_config_comes_from_the_environment_under_its_own_prefix(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_BACKDOOR_ENABLED", "true")
    monkeypatch.setenv("CLIFFRACER_BACKDOOR_PORT", "4321")
    cfg = BackdoorConfig()
    assert cfg.enabled is True and cfg.port == 4321


def test_a_constructor_override_beats_the_environment(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_BACKDOOR_PORT", "4321")
    assert BackdoorExtension(port=5555).config.port == 5555


async def test_a_disabled_extension_binds_nothing_and_says_so():
    class Svc(CliffracerService):
        backdoor = BackdoorExtension()

    svc = Svc(ServiceConfig(name="b"))
    await svc.container._setup_extensions()
    await svc.backdoor.start()
    try:
        assert svc.backdoor.health_details() == {"enabled": False}
        assert svc.backdoor._server is None, "nothing may be listening"
    finally:
        await svc.backdoor.stop()


async def test_an_enabled_extension_binds_and_reports_the_BOUND_port():
    """port=0 asks the OS for one, so health must report what was bound rather
    than what was configured -- reporting config.port would say 0 forever."""

    class Svc(CliffracerService):
        backdoor = BackdoorExtension(enabled=True, port=0)

    svc = Svc(ServiceConfig(name="b"))
    await svc.container._setup_extensions()
    await svc.backdoor.start()
    try:
        details = svc.backdoor.health_details()
        assert details["enabled"] is True
        assert isinstance(details["port"], int) and details["port"] > 0, details
        # and it is really listening
        _, writer = await asyncio.open_connection("127.0.0.1", details["port"])
        writer.close()
    finally:
        await svc.backdoor.stop()
        assert svc.backdoor.health_details()["port"] is None, "stop() must clear it"


def test_two_services_do_not_share_backdoor_state():
    """bind() is a shallow copy, so per-instance state is created in setup()."""

    class A(CliffracerService):
        backdoor = BackdoorExtension()

    class B(CliffracerService):
        backdoor = BackdoorExtension()

    a, b = A(ServiceConfig(name="a")), B(ServiceConfig(name="b"))
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        a.container._setup_extensions()
    )
    assert a.backdoor is not b.backdoor


# --- CLI tests -------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [("localhost:9999", ("localhost", 9999)), ("127.0.0.1:1", ("127.0.0.1", 1))],
)
def test_parse_endpoint_accepts_host_port(text, expected):
    assert parse_endpoint(text) == expected


@pytest.mark.parametrize("text", ["localhost", ":9999", "localhost:notaport"])
def test_parse_endpoint_refuses_anything_else(text):
    with pytest.raises(ValueError):
        parse_endpoint(text)


def test_the_cli_exits_2_on_a_malformed_endpoint(capsys):
    assert main(["localhost"]) == 2
    assert "expected host:port" in capsys.readouterr().err


def test_the_cli_exits_nonzero_when_nothing_is_listening(capsys):
    """A client that prints instructions and returns 0 is a false success.

    `BackdoorClient.connect` shells out to `nc` then `telnet`, and when both
    fail it PRINTS how to connect manually and returns normally -- so
    delegating straight to it would exit 0 with nothing connected. The CLI
    probes first, which is the only reason this can be asserted.

    The port is taken and released, so it is genuinely free rather than merely
    privileged: `127.0.0.1:1` would also fail for a caller without permission
    to reach it, which is a different reason and would pass on a broken probe.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]

    rc = main([f"127.0.0.1:{free}"])
    assert rc != 0, "nothing is listening; exiting 0 would be a false success"
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not connect" in err
    assert err.count("\n") == 1, f"one line, not a wall of instructions: {err!r}"


# --- the library classes, exported and constructed by nothing else ----------


def test_the_inspectors_are_constructible():
    class Svc(CliffracerService):
        pass

    svc = Svc(ServiceConfig(name="i"))
    assert ServiceInspector(svc) is not None
    assert NATSInspector(svc) is not None


async def test_the_console_binds_a_literal_address_not_the_name_localhost():
    """CI run 1425 caught this and the local suite could not.

    `start_server(host="localhost")` resolves the NAME. Where localhost has
    both loopback families that binds TWO sockets, and with port=0 each gets a
    DIFFERENT ephemeral port -- so `sockets[0]` reports a port only one family
    answers on, and `health_details()["port"]` names something you cannot
    connect to. tim resolves localhost to 127.0.0.1 alone, so the live-connect
    test below passes here whatever the host argument is; only CI's container
    had both.

    This asserts the argument that DECIDES, so it fails on either machine.
    """
    seen = {}
    real = asyncio.start_server

    async def spy(cb, /, *args, **kwargs):
        seen["host"] = kwargs.get("host", args[0] if args else None)
        return await real(cb, *args, **kwargs)

    class Svc(CliffracerService):
        backdoor = BackdoorExtension(enabled=True, port=0)

    svc = Svc(ServiceConfig(name="literal"))
    await svc.container._setup_extensions()
    asyncio.start_server = spy
    try:
        await svc.backdoor.start()
    finally:
        asyncio.start_server = real

    try:
        assert seen["host"] == "127.0.0.1", (
            f"bound host must be a literal address, got {seen['host']!r}; "
            "a name that resolves to two families splits the ephemeral port"
        )
        # and exactly one socket, which is what makes the reported port sound
        sockets = svc.backdoor._server.server.sockets
        assert len(sockets) == 1
        bound = sockets[0].getsockname()
        assert bound[0] == "127.0.0.1"
        assert svc.backdoor.health_details()["port"] == bound[1]
    finally:
        await svc.backdoor.stop()


def test_the_password_comes_from_CLIFFRACER_BACKDOOR_PASSWORD(monkeypatch):
    """Password is read from CLIFFRACER_BACKDOOR_PASSWORD via settings prefix."""
    monkeypatch.setenv("CLIFFRACER_BACKDOOR_PASSWORD", "from-the-environment")

    assert BackdoorConfig().password == "from-the-environment"


def test_CONTROL_the_old_unprefixed_name_is_ignored(monkeypatch):
    """Ensure unprefixed environment variable is ignored."""
    monkeypatch.delenv("CLIFFRACER_BACKDOOR_PASSWORD", raising=False)
    monkeypatch.setenv("BACKDOOR_PASSWORD", "should-be-ignored")

    assert BackdoorConfig().password is None

    from cliffracer_backdoor.backdoor import BackdoorServer

    server = BackdoorServer(service_instance=None, enabled=False)
    assert server.password is None, (
        "BackdoorServer still reads the unprefixed name; the settings model is the only source"
    )


def test_a_constructor_password_still_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_BACKDOOR_PASSWORD", "from-the-environment")

    assert BackdoorConfig(password="explicit").password == "explicit"


@pytest.mark.asyncio
async def test_backdoor_lockout_drains_message_without_type_error():
    """Verify lockout message is written and drained without awaiting writer.write()."""
    import time
    from unittest.mock import AsyncMock, MagicMock

    from cliffracer_backdoor.backdoor import BackdoorServer

    server = BackdoorServer(service_instance=None, enabled=False)
    now = time.time()
    server.failed_auth_attempts["127.0.0.1"] = [now - 10, now - 5, now - 1]

    mock_reader = AsyncMock()
    mock_writer = MagicMock()
    mock_writer.get_extra_info.return_value = ("127.0.0.1", 12345)
    mock_writer.drain = AsyncMock()
    mock_writer.wait_closed = AsyncMock()

    await server._handle_client(mock_reader, mock_writer)
    mock_writer.write.assert_called_with(b"Too many failed attempts. Try again later.\n")
    mock_writer.drain.assert_awaited()
    assert mock_writer.close.called


@pytest.mark.asyncio
async def test_stop_cancels_active_clients():
    import asyncio

    from cliffracer_backdoor.backdoor import BackdoorServer

    from cliffracer import CliffracerService, ServiceConfig

    svc = CliffracerService(ServiceConfig(name="test"))
    backdoor = BackdoorServer(svc, port=0, enabled=True, password="test")
    port = await backdoor.start()

    try:
        # Create connection
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        # Give it a moment to accept the connection
        await asyncio.sleep(0.1)

        # Stop the server
        await backdoor.stop()

        # Reading should hit EOF and return the auth prompt (or empty), without hanging.
        try:
            data = await reader.read()
            assert b"Password:" in data or not data
        except ConnectionResetError:
            pass

    finally:
        if backdoor.server:
            backdoor.server.close()

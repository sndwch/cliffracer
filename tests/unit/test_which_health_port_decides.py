"""Which port a health listener binds, and which argument is ignored.

`HealthListener(service, host, port)` looks like the port is the port. It is
not, for any service with a config: `start()` re-reads `config.health_port`
so a runtime override is honoured, and the constructor's value is discarded.
Every `CliffracerService` has a config, so the argument is dead in production
-- the one real construction site, `service.py`, passes `config.health_port`
to it, which `start()` then reads again from the config.

AN AUTHOR HAS BEEN MISLED BY IT, which is why this exists rather than a
docstring alone:

- `tests/repo/test_core_imports_no_web_stack.py` built its probe with
  `HealthListener(service, "127.0.0.1", 0)` and a default config, and bound
  8000 in a subprocess. It failed whenever anything else on the host held that
  port, and now asks for `health_port=0` on the config.

THE DISCRIMINATOR IS AN OCCUPIED PORT, not a port number read back. Comparing
`listener.port` against a number obtained earlier races with every other
process on the host; a port that is already bound cannot be bound twice, so
"which one did it try" is answered by which attempt fails.

Every test here disables `_test_port_override`, the autouse fixture in
conftest.py that makes the rest of the suite bind ephemeral ports. These are
the tests that must see the real resolution, and they take a port for a
fraction of a second each -- an ephemeral one they were handed, never a fixed
number.
"""

from __future__ import annotations

import asyncio
import errno
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit


class NoConfig:
    """A service object with no `config` at all -- the only shape for which the
    constructor's port is what decides. `getattr(self.service, "config", None)`
    is how `start()` asks."""

    async def health_check(self) -> dict[str, Any]:
        return {"status": "healthy"}


@pytest.fixture
def real_resolution(monkeypatch):
    """Turn off the suite-wide ephemeral-port override for this test."""
    monkeypatch.setattr(HealthListener, "_test_port_override", None)


async def _occupied() -> tuple[asyncio.AbstractServer, int]:
    """A port this process holds, so a second bind of it must fail."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


# --- a configured service: the config decides, both directions ---------------


async def test_a_configured_service_ignores_the_constructor_port(real_resolution):
    """The trap, stated as the thing that happens: the constructor gets 0 and
    the listener binds the configured port anyway.

    The configured port is the occupied one, so "it bound the config's port" is
    observable as a refusal rather than as a number.
    """
    dummy, taken = await _occupied()
    try:
        svc = CliffracerService(ServiceConfig(name="cfg_wins", health_port=taken))
        listener = HealthListener(svc, "127.0.0.1", 0)

        with pytest.raises(OSError) as refused:
            await listener.start()

        assert refused.value.errno == errno.EADDRINUSE, refused.value
        assert listener.port is None
    finally:
        dummy.close()
        await dummy.wait_closed()


async def test_a_configured_service_ignores_an_occupied_constructor_port(real_resolution):
    """The same precedence the other way round, so it is not an artefact of
    which value happened to be 0.

    The constructor gets the occupied port and the config asks for any free
    one. If the argument decided, this would raise.
    """
    dummy, taken = await _occupied()
    try:
        svc = CliffracerService(ServiceConfig(name="cfg_wins_too", health_port=0))
        listener = HealthListener(svc, "127.0.0.1", taken)

        await listener.start()
        try:
            assert listener.port is not None
            assert listener.port != taken, "the constructor's port was honoured"
        finally:
            await listener.stop()
    finally:
        dummy.close()
        await dummy.wait_closed()


# --- no config: the argument is what decides --------------------------------


async def test_a_service_without_a_config_binds_the_constructor_port(real_resolution):
    """The one case where the argument is live, and nothing tested it before.

    Asserted through a refusal, for the same reason as above: the argument
    names a port this process already holds.
    """
    dummy, taken = await _occupied()
    try:
        listener = HealthListener(NoConfig(), "127.0.0.1", taken)

        with pytest.raises(OSError) as refused:
            await listener.start()

        assert refused.value.errno == errno.EADDRINUSE, refused.value
    finally:
        dummy.close()
        await dummy.wait_closed()


async def test_a_service_without_a_config_and_no_port_gets_a_free_one(real_resolution):
    """`port` is omitted entirely, which the signature now allows.

    `None` means the same as 0 -- ask the operating system -- so this must bind
    rather than raise, and must answer a probe on whatever it was given.
    """
    listener = HealthListener(NoConfig(), "127.0.0.1")

    await listener.start()
    try:
        assert listener.port is not None and listener.port > 0

        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        writer.write(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=10.0)
        writer.close()

        assert b" 200 " in response, response
    finally:
        await listener.stop()


# --- controls ----------------------------------------------------------------


async def test_CONTROL_the_conftest_override_is_what_makes_the_suite_safe():
    """Without `real_resolution`, and this is why the other tests in the suite
    can pass 0 to the constructor and come to no harm.

    It is also the mechanism that hid the defect from two authors: the argument
    they passed did nothing, and the fixture did the work they credited it
    with. Note the absence of the fixture here -- this test wants the override
    ON.
    """
    dummy, taken = await _occupied()
    try:
        svc = CliffracerService(ServiceConfig(name="override_saves", health_port=taken))
        listener = HealthListener(svc, "127.0.0.1", taken)

        await listener.start()
        try:
            assert listener.port not in (taken, 0)
        finally:
            await listener.stop()
    finally:
        dummy.close()
        await dummy.wait_closed()


async def test_CONTROL_asyncio_treats_an_unspecified_port_as_any_free_port():
    """The assumption the omitted-port case rests on, asserted rather than trusted.

    `start()` passes `None` straight to `asyncio.start_server` when no port and
    no config name one. Normalising it to 0 first was code no test could
    distinguish, so it went; this is what holds the behaviour up in its place.
    If a future event loop treated `None` as something other than "any free
    port", this fails here rather than as a puzzling bind in the listener.
    """
    for unspecified in (None, 0):
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", unspecified)
        try:
            bound = server.sockets[0].getsockname()[1]
            assert bound > 0, (unspecified, bound)
        finally:
            server.close()
            await server.wait_closed()


async def test_CONTROL_an_occupied_port_really_cannot_be_bound_twice(real_resolution):
    """The premise every refusal above rests on.

    If `SO_REUSEPORT` or anything else let two listeners share a port, each
    `pytest.raises(OSError)` above would be asserting something that never
    happens for the reason it claims.
    """
    dummy, taken = await _occupied()
    try:
        with pytest.raises(OSError) as refused:
            await asyncio.start_server(lambda r, w: None, "127.0.0.1", taken)

        assert refused.value.errno == errno.EADDRINUSE
    finally:
        dummy.close()
        await dummy.wait_closed()


def test_CONTROL_the_one_production_call_site_passes_the_config_value():
    """So "the argument is dead in production" is read off the source, not asserted.

    `service.py` constructs the listener with `config.health_port` -- the same
    value `start()` re-reads -- which is why the precedence has never shown up
    as a bug in the library itself.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "src" / "cliffracer" / "core" / "service.py"
    text = source.read_text()

    assert "HealthListener(self, config.health_host, config.health_port)" in text, (
        "the production construction site has changed shape; the claim in this "
        "file's docstring about the argument being dead needs re-checking"
    )

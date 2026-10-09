"""Shutdown is bounded by its own grace, not by what a client on the health
port is doing.

`Server.wait_closed()` does not return until every live handler has finished,
and `stop()` awaited it unbounded. The header read was `wait_for(readline(),
timeout=5)` inside a loop with no total budget, so the five seconds reset on
every line: a client writing one short header every couple of seconds held its
handler open for as long as it liked. `LifecycleManager` awaits this as step 2
of shutdown, **before** it cancels subscriptions, so the service went on
consuming messages while one socket decided when it could stop.

MEASURED BEFORE THE FIX, because the two client shapes are not equally bad and
the issue's title fits only one of them:

    idle (connects, sends nothing)   stop() returned after  4.81s
    dripping (one header every 2s)   stop() returned after 22.82s

The idle client is bounded by the per-line timeout -- a 4.8s stall, real but
finite. **Only the dripping client was unbounded**, and it tracked its drip
exactly: dripping for 20s made `stop()` return 20s later.

THE FIX HAS TWO INDEPENDENT PARTS, and the first version of these tests could
not tell them apart. `REQUEST_DEADLINE_SECONDS` bounds the handler, so with the
deadline in place an unbounded `stop()` still returns in about five seconds --
which fits under any bound loose enough not to flake, and the test named for
shutdown passed with shutdown's bound removed. So the tests below that are
about shutdown raise the request budget past every bound they assert
(`held_open`), which turns them back into completion-versus-never: the handler
will not end on its own inside the test, so a `stop()` that waits for it does
not return. The one test that is about the budget itself uses the real value.

None of these bounds measures how fast shutdown is -- ten seconds against an
expectation of about one -- so a loaded host does not make them red.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core import health_listener as health_listener_module
from cliffracer.core.health_listener import (
    MAX_HEADER_LINES,
    REQUEST_DEADLINE_SECONDS,
    SHUTDOWN_GRACE_SECONDS,
    HealthListener,
)

pytestmark = pytest.mark.unit

# Separates "stop() returns" from "stop() never returns". The fix bounds
# shutdown at two graces, so this is several times the expectation.
COMPLETES_WITHIN = 10.0

# A request budget longer than every bound in this file, so a handler under
# `held_open` cannot end by itself while a test is running. Shutdown then has to
# be what ends it.
HELD_OPEN_BUDGET = 600.0

# One drip well inside the pre-fix per-line timeout, so the pre-fix code keeps
# resetting its budget rather than timing out between lines.
DRIP_EVERY = 0.2

# A health check slow enough to still be running when stop() is called, and
# short enough to finish inside the grace.
IN_FLIGHT_CHECK_TAKES = 0.3


@pytest.fixture
def held_open(monkeypatch):
    """Raise the request budget past every bound here.

    The point of the tests that use this is that shutdown does not wait on a
    client. Leaving the real budget in place lets the handler expire on its own
    inside the bound, which is how the earlier version of this file passed with
    the whole of `stop()`'s bound removed.
    """
    monkeypatch.setattr(health_listener_module, "REQUEST_DEADLINE_SECONDS", HELD_OPEN_BUDGET)


async def _listener(service: Any = None) -> HealthListener:
    """A listener on an OS-assigned port, so no test binds a fixed one."""
    if service is None:
        service = CliffracerService(
            ServiceConfig(name="held_port_svc", health_port=0, health_listener=False)
        )
    listener = HealthListener(service, "127.0.0.1", 0)
    listener._test_port_override = 0
    await listener.start()
    assert listener.port is not None, "the fixture never bound a port"
    return listener


async def _connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


async def _drip(writer: asyncio.StreamWriter) -> None:
    """A header line every DRIP_EVERY seconds, forever."""
    while True:
        writer.write(b"X-Pad: 1\r\n")
        with contextlib.suppress(Exception):
            await writer.drain()
        await asyncio.sleep(DRIP_EVERY)


async def _socket_ended(reader: asyncio.StreamReader, *, within: float) -> bool:
    """True if the peer ended the connection -- EOF, or a reset from an abort."""
    try:
        return await asyncio.wait_for(reader.read(4096), timeout=within) == b""
    except (ConnectionResetError, BrokenPipeError):
        return True
    except TimeoutError:
        return False


def _close(*writers: asyncio.StreamWriter) -> None:
    for writer in writers:
        with contextlib.suppress(Exception):
            writer.close()


async def _stop_and_cancel(listener: HealthListener, *tasks: asyncio.Task[None]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(listener.stop(), timeout=COMPLETES_WITHIN)


# --- shutdown does not wait on a client --------------------------------------


@pytest.mark.asyncio
async def test_a_dripping_client_cannot_hold_shutdown_open(held_open):
    """The completion-versus-never case.

    The dripper never stops on its own and, under `held_open`, neither does its
    handler. Before the fix `stop()` returned only once a client stopped -- so
    with this client there was no time at which it returned.
    """
    listener = await _listener()
    _, writer = await _connect(listener.port)
    dripper = asyncio.create_task(_drip(writer))
    try:
        await asyncio.sleep(DRIP_EVERY * 2)

        await asyncio.wait_for(listener.stop(), timeout=COMPLETES_WITHIN)
    finally:
        await _stop_and_cancel(listener, dripper)
        _close(writer)


@pytest.mark.asyncio
async def test_an_idle_client_cannot_hold_shutdown_open(held_open):
    """Connects and sends nothing -- the shape named in the issue.

    Bounded even before the fix, by the per-line timeout, which is why the
    measurement above reads 4.81s rather than a hang. Under `held_open` that
    bound is past the end of this test, so the stall becomes a hang and the
    assertion is about shutdown rather than about the length of a timeout.
    """
    listener = await _listener()
    _, writer = await _connect(listener.port)
    try:
        await asyncio.sleep(0.1)

        await asyncio.wait_for(listener.stop(), timeout=COMPLETES_WITHIN)
    finally:
        _close(writer)


@pytest.mark.asyncio
async def test_dripping_clients_do_not_add_up(held_open):
    """The bound is on shutdown, not on each connection.

    Ten drippers must not cost ten graces: `stop()` aborts what is left in one
    pass rather than waiting per socket.
    """
    listener = await _listener()
    writers: list[asyncio.StreamWriter] = []
    drippers: list[asyncio.Task[None]] = []
    try:
        for _ in range(10):
            _, writer = await _connect(listener.port)
            writers.append(writer)
            drippers.append(asyncio.create_task(_drip(writer)))
        await asyncio.sleep(DRIP_EVERY * 2)

        await asyncio.wait_for(listener.stop(), timeout=COMPLETES_WITHIN)
    finally:
        await _stop_and_cancel(listener, *drippers)
        _close(*writers)


@pytest.mark.asyncio
async def test_the_held_socket_is_gone_once_shutdown_returns(held_open):
    """`stop()` returning is not the same as the connection being released.

    Without the abort, the two grace waits expire, `stop()` drops its reference
    to the server and returns on time -- with the handler still running against
    a live socket. Shutdown would report done while the thing it waited for was
    still there, which is the same defect made quieter. Asserted from the
    client's side, because that is what can see the socket.
    """
    listener = await _listener()
    reader, writer = await _connect(listener.port)
    dripper = asyncio.create_task(_drip(writer))
    try:
        await asyncio.sleep(DRIP_EVERY * 2)

        await asyncio.wait_for(listener.stop(), timeout=COMPLETES_WITHIN)

        ended = await _socket_ended(reader, within=COMPLETES_WITHIN)
        assert ended, (
            "stop() returned while the dripping client's connection was still "
            "open, so the handler outlived the shutdown that waited for it"
        )
    finally:
        await _stop_and_cancel(listener, dripper)
        _close(writer)


# --- the handler bounds itself, with no shutdown involved ---------------------


@pytest.mark.asyncio
async def test_a_dripping_client_is_cut_off_even_if_nothing_shuts_down():
    """The request budget, on the real value, asserted on its own.

    A bounded `stop()` alone would hide this: shutdown would return on time
    while a handler stayed alive for as long as a client kept dripping, holding
    a connection. So the budget is asserted where nothing is shutting down --
    the client must be answered or dropped, rather than read forever.
    """
    listener = await _listener()
    reader, writer = await _connect(listener.port)
    dripper = asyncio.create_task(_drip(writer))
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        # a response or an EOF; either ends the handler, being read forever does not
        await asyncio.wait_for(reader.read(4096), timeout=COMPLETES_WITHIN)
        took = loop.time() - started

        # Upper bound. CI p99 5 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 5 s,
        # 2376x the overshoot.
        assert took < REQUEST_DEADLINE_SECONDS * 2, (
            f"the handler took {took:.2f}s to give up on a client dripping one "
            f"header every {DRIP_EVERY}s, against a {REQUEST_DEADLINE_SECONDS}s "
            "budget for the whole request"
        )
    finally:
        await _stop_and_cancel(listener, dripper)
        _close(writer)


@pytest.mark.asyncio
async def test_a_flood_of_headers_is_refused_rather_than_read():
    """A deadline alone does not bound the work: a client can fill five seconds
    with as many header lines as the socket carries. The cap is what makes the
    handler's work finite as well as its wall-clock."""
    listener = await _listener()
    reader, writer = await _connect(listener.port)
    try:
        writer.write(b"GET /live HTTP/1.1\r\n")
        writer.write(b"X-Pad: 1\r\n" * (MAX_HEADER_LINES + 10))
        await writer.drain()

        response = await asyncio.wait_for(reader.read(4096), timeout=COMPLETES_WITHIN)

        assert b" 431 " in response, response
    finally:
        _close(writer)
        await asyncio.wait_for(listener.stop(), timeout=COMPLETES_WITHIN)


# --- the control: a request that WILL finish still gets its answer ------------


class SlowToAnswer:
    """A service whose health check is still running when shutdown starts."""

    def __init__(self):
        self.config = ServiceConfig(name="slow_answer", health_port=0, health_listener=False)
        self.checks_started = 0

    async def health_check(self) -> dict[str, Any]:
        self.checks_started += 1
        await asyncio.sleep(IN_FLIGHT_CHECK_TAKES)
        return {"status": "healthy", "service": "slow_answer"}


@pytest.mark.asyncio
async def test_CONTROL_a_request_in_flight_when_stop_is_called_still_gets_its_response():
    """Shutdown takes sockets from clients that will not finish, not from clients
    that will.

    Without this, "stop() always returns quickly" is satisfied by aborting every
    connection the moment shutdown begins -- which would turn a probe that was
    mid-response into a failure on every ordinary shutdown. The check here is
    genuinely in flight: it sleeps for IN_FLIGHT_CHECK_TAKES, which starts
    before `stop()` and ends after it.
    """
    service = SlowToAnswer()
    listener = await _listener(service)
    reader, writer = await _connect(listener.port)
    try:
        writer.write(b"GET /ready HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        # wait until the handler is inside health_check, not merely connected
        for _ in range(100):
            if service.checks_started:
                break
            await asyncio.sleep(0.01)
        assert service.checks_started == 1, (
            "the request never reached health_check, so this measures a "
            "connection rather than a request in flight"
        )

        stop_task = asyncio.create_task(listener.stop())
        response = await asyncio.wait_for(reader.read(4096), timeout=COMPLETES_WITHIN)
        await asyncio.wait_for(stop_task, timeout=COMPLETES_WITHIN)

        assert b" 200 " in response, response
        assert b"healthy" in response, response
    finally:
        _close(writer)


@pytest.mark.asyncio
async def test_CONTROL_the_same_request_answers_when_nothing_is_shutting_down():
    """The same service, the same request, no concurrent `stop()`.

    So the 200 above is not something this fixture returns regardless, and the
    only difference between the two is when shutdown starts.
    """
    service = SlowToAnswer()
    listener = await _listener(service)
    reader, writer = await _connect(listener.port)
    try:
        writer.write(b"GET /ready HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()

        response = await asyncio.wait_for(reader.read(4096), timeout=COMPLETES_WITHIN)

        assert b" 200 " in response, response
        assert b"healthy" in response, response
    finally:
        _close(writer)
        await asyncio.wait_for(listener.stop(), timeout=COMPLETES_WITHIN)


# --- controls on the instrument ----------------------------------------------


@pytest.mark.asyncio
async def test_CONTROL_the_patched_budget_reaches_the_handler(monkeypatch):
    """Four tests above rest on `held_open` changing what the handler does.

    `_handle` reads the budget from module scope at request time, so patching
    the module reaches it -- but if that ever stopped being true, those four
    would go on passing while asserting the thing they were written to exclude.
    Shown with a short budget rather than a long one, because a short one is
    observable inside a test: patched to a fraction of a second, a dripping
    client is dropped in a fraction of a second.
    """
    monkeypatch.setattr(health_listener_module, "REQUEST_DEADLINE_SECONDS", 0.2)
    listener = await _listener()
    reader, writer = await _connect(listener.port)
    dripper = asyncio.create_task(_drip(writer))
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(reader.read(4096), timeout=COMPLETES_WITHIN)
        took = loop.time() - started

        # Upper bound. CI p99 0.202 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.2
        # s, 1199x the overshoot; below 5 s (the unpatched 5.0 s deadline).
        assert took < 2.0, (
            f"a 0.2s budget took {took:.2f}s to bite, so the handler is not "
            f"reading the patched value (the real budget is "
            f"{REQUEST_DEADLINE_SECONDS}s)"
        )
    finally:
        await _stop_and_cancel(listener, dripper)
        _close(writer)


def test_CONTROL_the_grace_leaves_room_under_the_bound_these_tests_use():
    """Otherwise the bounds above would pass by being looser than the grace,
    and a later raise of the grace would turn them red for a reason that has
    nothing to do with a held socket. `stop()` spends up to two graces when it
    has to abort, so that is what has to fit."""
    assert SHUTDOWN_GRACE_SECONDS * 2 < COMPLETES_WITHIN, (
        f"a grace of {SHUTDOWN_GRACE_SECONDS}s costs up to "
        f"{SHUTDOWN_GRACE_SECONDS * 2}s of shutdown, which does not fit under "
        f"the {COMPLETES_WITHIN}s bound these tests assert"
    )


def test_CONTROL_the_held_open_budget_is_past_every_bound_here():
    """`held_open` only works if the handler cannot expire inside a test."""
    assert HELD_OPEN_BUDGET > COMPLETES_WITHIN * 2, (
        f"a {HELD_OPEN_BUDGET}s budget is not clear of a {COMPLETES_WITHIN}s "
        "bound, so a handler could end on its own and a test about shutdown "
        "would pass without shutdown bounding anything"
    )

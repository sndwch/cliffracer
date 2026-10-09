"""`/ready` asks the broker for a round trip, bounded and cached, instead of reading nats-py's flag alone.

nats-py clears its `is_connected` flag only when its ping loop gives up, which for a connection that
went silent without being reset takes minutes. The readiness check now sends the broker a PING while
the flag says connected, bounded by `broker_probe_timeout`; one that is not answered in time makes
the status `disconnected`. The result, a failure included, is reused for `broker_probe_cache`
seconds and callers that arrive while a round trip is in flight share it, so a burst of probes costs
one round trip. `broker_probe_timeout=None` turns it off.

THE PONG FUTURE IS NEVER CANCELLED, and the fake client below holds the probe to it. nats-py pops
the oldest future in its queue when a PONG arrives and calls `set_result` on it, and a future that
was cancelled while it waited raises `InvalidStateError` there, inside the read loop, which ends
without telling the client. `_Client._answer` does exactly that and records it as `reader_died`.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from loguru import logger
from pydantic import ValidationError

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core import broker_probe

pytestmark = pytest.mark.unit


class _Client:
    """A client with nats-py's flags and PING hook, whose PONGs the test controls.

    `delay` is how long the broker takes to answer; `hold` keeps every answer back until
    `release()`, which answers them oldest first, as a healed path does. `raises` makes the send
    itself fail.
    """

    is_closed = False
    is_connected = True
    is_draining = False
    is_connecting = False

    def __init__(self, *, delay: float = 0.0, hold: bool = False, raises: Exception | None = None):
        self.pings = 0
        self.delay = delay
        self.hold = hold
        self.raises = raises
        self._pongs: list[asyncio.Future] = []
        self.reader_died = False

    async def _send_ping(self, future: asyncio.Future | None = None) -> None:
        if self.raises is not None:
            raise self.raises
        self.pings += 1
        future = future if future is not None else asyncio.get_running_loop().create_future()
        self._pongs.append(future)
        if not self.hold:
            asyncio.get_running_loop().call_later(self.delay, self._answer)

    def _answer(self) -> None:
        """What nats-py's `_process_pong` does with a PONG."""
        if not self._pongs:
            return
        future = self._pongs.pop(0)
        try:
            future.set_result(True)
        except asyncio.InvalidStateError:
            self.reader_died = True

    def release(self) -> None:
        for _ in range(len(self._pongs)):
            self._answer()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _service(client, **config) -> CliffracerService:
    svc = CliffracerService(ServiceConfig(name="ready", health_port=0, **config))
    svc._running = True
    svc.nc = client
    return svc


async def test_a_broker_that_answers_is_healthy_and_the_round_trip_is_reported():
    client = _Client()
    svc = _service(client)

    health = await svc.health_check()

    assert client.pings == 1
    assert health["status"] == "healthy" and health["nats_connected"] is True
    assert isinstance(health["nats_rtt_ms"], float) and health["nats_rtt_ms"] >= 0


async def test_a_round_trip_that_is_not_answered_in_the_bound_makes_the_service_disconnected():
    client = _Client(delay=5.0)
    svc = _service(client, broker_probe_timeout=0.1)

    began = time.monotonic()
    health = await svc.health_check()

    # Upper bound. CI p99 0.101 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.1 s,
    # 766x the overshoot; below 5 s (the client's delay=5.0).
    assert time.monotonic() - began < 1.0, "the check waited longer than its bound"
    assert health["status"] == "disconnected"
    assert health["nats_connected"] is False and health["nats_rtt_ms"] is None
    assert health["broker_state"] == "connected", "the flag still says connected: that is the point"


async def test_a_send_that_raises_makes_the_service_disconnected():
    from nats.errors import ConnectionClosedError

    svc = _service(_Client(raises=ConnectionClosedError()))

    health = await svc.health_check()

    assert health["status"] == "disconnected" and health["nats_connected"] is False


async def test_the_probe_never_cancels_the_pong_future_it_leaves_behind():
    """A cancelled future at the head of nats-py's queue kills the read loop at the next PONG."""
    client = _Client(delay=1.0)
    svc = _service(client, broker_probe_timeout=0.1, broker_probe_cache=0)

    for _ in range(4):
        assert (await svc.health_check())["status"] == "disconnected"

    assert len(client._pongs) == 4
    assert not any(future.cancelled() for future in client._pongs)
    await asyncio.sleep(1.6)  # the broker answers them all, late
    assert client.reader_died is False, "a PONG found a cancelled future: the reader would be dead"


async def test_after_a_heal_the_held_pongs_arrive_in_order_and_the_next_probe_is_answered():
    """TCP holds the bytes and delivers them in order: the old PINGs' PONGs first, then the new one."""
    client = _Client(hold=True)
    svc = _service(client, broker_probe_timeout=0.1, broker_probe_cache=0)
    for _ in range(3):
        assert (await svc.health_check())["status"] == "disconnected"

    client.release()  # the three held PONGs arrive
    client.hold = False
    client.delay = 0.0
    health = await svc.health_check()  # its own PING is answered as well

    assert health["status"] == "healthy"
    assert client.reader_died is False
    assert client._pongs == []


async def test_a_broker_slower_than_the_bound_is_never_reported_healthy():
    """Every PONG arrives, in order, 0.15 s after its PING; the bound is 0.1 s.

    The PONG for one probe lands inside the NEXT probe's window. If that counted as the next probe's
    answer, a broker that always takes longer than the bound would read healthy part of the time,
    which is the opposite of what the bound says.
    """
    client = _Client(delay=0.15)
    svc = _service(client, broker_probe_timeout=0.1, broker_probe_cache=0)

    statuses = [(await svc.health_check())["status"] for _ in range(8)]

    assert statuses == ["disconnected"] * 8, statuses
    assert client.reader_died is False


async def test_a_held_pong_from_an_earlier_ping_does_not_answer_the_next_probe():
    """An asymmetric path: the reply to an old PING gets through while the new PING is unanswered."""
    client = _Client(hold=True)
    svc = _service(client, broker_probe_timeout=0.3, broker_probe_cache=0)
    assert (await svc.health_check())["status"] == "disconnected"

    probing = asyncio.create_task(svc.health_check())
    await asyncio.sleep(0.05)
    client._answer()  # the earlier PING's PONG arrives; the new one's does not
    health = await probing

    assert health["status"] == "disconnected"
    assert client.reader_died is False


async def test_probes_stop_sending_when_too_many_are_unanswered_and_resume_after_a_reconnect(
    monkeypatch,
):
    monkeypatch.setattr(broker_probe, "MAX_UNANSWERED", 3)
    client = _Client(hold=True)
    svc = _service(client, broker_probe_timeout=0.05, broker_probe_cache=0)

    for _ in range(6):
        assert (await svc.health_check())["status"] == "disconnected"
    assert client.pings == 3, "the queue grew past the cap"

    client._pongs.clear()  # a reconnect empties nats-py's queue
    client.hold = False
    health = await svc.health_check()

    assert health["status"] == "healthy" and client.pings == 4


async def test_a_burst_of_checks_inside_the_cache_period_costs_one_round_trip():
    client = _Client()
    svc = _service(client)

    for _ in range(10):
        await svc.health_check()

    assert client.pings == 1


async def test_a_check_after_the_cache_period_asks_again():
    client = _Client()
    svc = _service(client, broker_probe_cache=1.0)
    clock = _Clock()
    svc._broker_probe._clock = clock

    await svc.health_check()
    clock.now += 0.9
    await svc.health_check()
    assert client.pings == 1
    clock.now += 0.2
    await svc.health_check()

    assert client.pings == 2


async def test_a_cache_period_of_zero_asks_on_every_check():
    client = _Client()
    svc = _service(client, broker_probe_cache=0)

    for _ in range(3):
        await svc.health_check()

    assert client.pings == 3


async def test_checks_that_arrive_while_a_round_trip_is_in_flight_share_it():
    client = _Client(delay=0.2)
    svc = _service(client, broker_probe_cache=0)

    answers = await asyncio.gather(*[svc.health_check() for _ in range(10)])

    assert client.pings == 1
    assert {a["status"] for a in answers} == {"healthy"}


async def test_a_caller_that_goes_away_does_not_cancel_the_round_trip_the_others_wait_on():
    client = _Client(delay=0.2)
    svc = _service(client, broker_probe_cache=0)
    leaving = asyncio.create_task(svc.health_check())
    staying = asyncio.create_task(svc.health_check())
    await asyncio.sleep(0.05)

    leaving.cancel()
    answer = await staying

    assert answer["status"] == "healthy" and client.pings == 1
    assert client.reader_died is False


async def test_a_failure_is_cached_too_and_the_next_answer_after_it_recovers():
    client = _Client(hold=True)
    svc = _service(client, broker_probe_timeout=0.05, broker_probe_cache=1.0)
    clock = _Clock()
    svc._broker_probe._clock = clock

    first = await svc.health_check()
    second = await svc.health_check()
    assert (first["status"], second["status"], client.pings) == ("disconnected",) * 2 + (1,)

    client.hold = False
    client.release()
    clock.now += 1.1
    recovered = await svc.health_check()

    assert recovered["status"] == "healthy" and client.pings == 2


async def test_the_change_of_answer_is_logged_and_a_steady_one_is_not():
    client = _Client(raises=RuntimeError("down"))
    svc = _service(client, broker_probe_cache=0)
    lines: list[tuple[str, str]] = []
    sink = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])))
    try:
        await svc.health_check()
        await svc.health_check()
        client.raises = None
        await svc.health_check()
        await svc.health_check()
    finally:
        logger.remove(sink)

    said = [(level, msg) for level, msg in lines if "round trip" in msg]
    assert [level for level, _ in said] == ["WARNING", "INFO"], said
    assert "reporting disconnected" in said[0][1]


async def test_with_the_probe_turned_off_the_client_is_never_asked_and_the_flag_decides():
    client = _Client(delay=5.0)
    svc = _service(client, broker_probe_timeout=None)

    health = await svc.health_check()

    assert client.pings == 0
    assert health["status"] == "healthy" and health["nats_rtt_ms"] is None


async def test_a_client_without_the_ping_hook_is_not_asked_and_the_flag_decides(monkeypatch):
    """A double with only flags, or a nats-py that dropped the hook: readiness reads the flag."""

    class Flags:
        is_closed, is_connected, is_draining, is_connecting = False, True, False, False

    monkeypatch.setattr(broker_probe, "_warned_unavailable", False)
    svc = _service(Flags())
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        first = await svc.health_check()
        second = await svc.health_check()
    finally:
        logger.remove(sink)

    assert first["status"] == second["status"] == "healthy" and first["nats_rtt_ms"] is None
    assert len([line for line in lines if "_send_ping" in line]) == 1, lines


@pytest.mark.parametrize("state", ["stopped", "connecting", "disconnected"])
async def test_the_client_is_not_asked_unless_the_service_runs_and_the_flag_says_connected(state):
    client = _Client()
    svc = _service(client)
    if state == "stopped":
        svc._running = False
    elif state == "connecting":
        client.is_connected, client.is_connecting = False, True
    else:
        client.is_connected = False

    health = await svc.health_check()

    assert client.pings == 0
    assert health["status"] == state and health["nats_rtt_ms"] is None


async def test_liveness_never_asks_the_broker():
    client = _Client(delay=5.0)
    svc = _service(client)

    svc.liveness_check()

    assert client.pings == 0


def test_nats_py_still_has_the_hook_and_the_queue_the_probe_relies_on():
    """The probe uses `_send_ping` and `_pongs`, which are not public. If nats-py drops either,
    this goes red here instead of readiness silently reading the flag alone."""
    from nats.aio.client import Client

    client = Client()

    assert callable(getattr(client, "_send_ping", None))
    assert isinstance(client._pongs, list)
    assert broker_probe.can_ping(client)


def test_the_defaults_are_two_seconds_and_one_second():
    config = ServiceConfig(name="ready")

    assert (config.broker_probe_timeout, config.broker_probe_cache) == (2.0, 1.0)


@pytest.mark.parametrize("value", [0, -1])
def test_a_probe_timeout_that_is_not_positive_is_refused(value):
    with pytest.raises(
        ValidationError, match=r"broker_probe_timeout\s+Input should be greater than 0"
    ):
        ServiceConfig(name="ready", broker_probe_timeout=value)


def test_a_negative_cache_period_is_refused_and_zero_is_accepted():
    with pytest.raises(
        ValidationError, match=r"broker_probe_cache\s+Input should be greater than or equal to 0"
    ):
        ServiceConfig(name="ready", broker_probe_cache=-1)

    assert ServiceConfig(name="ready", broker_probe_cache=0).broker_probe_cache == 0

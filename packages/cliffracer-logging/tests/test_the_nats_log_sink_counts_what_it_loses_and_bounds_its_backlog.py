"""The NATS log sink says what it lost, and does not queue without limit.

The sink hands each record to the event loop from loguru's writer thread. What it does
after that is where records go missing: a refused publish, a loop that has closed, a
broker slower than the log rate. Each of those is counted where `/health` can read it,
the backlog is bounded, and a coroutine that never got scheduled is closed instead of
being left to be warned about once per log line.
"""

import asyncio
import gc
import warnings

import pytest
from cliffracer_logging import LoggingConfig, LoggingExtension
from cliffracer_logging.config import NatsSinkStats
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.testing import wait_until

pytestmark = pytest.mark.unit

CONFIG = ServiceConfig(name="sink_probe", health_port=0)


class DownNats:
    async def publish(self, subject: str, payload: bytes) -> None:
        raise ConnectionError("no connection")


class RecordingNats:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.payloads.append(payload)


class FlakyNats:
    """A broker that refuses publishes while `down` is set."""

    def __init__(self) -> None:
        self.down = True

    async def publish(self, subject: str, payload: bytes) -> None:
        if self.down:
            raise ConnectionError("no connection")


class BlockedNats:
    """A broker that accepts a publish only when told to."""

    def __init__(self) -> None:
        self.started = 0
        self.release = asyncio.Event()

    async def publish(self, subject: str, payload: bytes) -> None:
        self.started += 1
        await self.release.wait()


@pytest.fixture
def isolated_logger():
    logger.remove()
    yield
    logger.remove()


def _add(nc, *, stats: NatsSinkStats, **kwargs) -> int:
    return LoggingConfig.add_nats_sink("sink_probe", nc, config=CONFIG, stats=stats, **kwargs)


async def _drained(stats: NatsSinkStats, *, lines: int) -> None:
    """Wait until every line logged has been published, lost or is still pending."""
    logger.complete()
    await wait_until(
        lambda: sum(stats.snapshot()[k] for k in ("published", "failed", "dropped", "pending"))
        >= lines,
        within=5.0,
        reason="the sink accounted for every record it was handed",
    )


async def test_a_refused_publish_is_counted_and_named(isolated_logger):
    stats = NatsSinkStats()
    sink_id = _add(DownNats(), stats=stats)

    for _ in range(3):
        logger.bind(service="sink_probe").info("a line")
    await wait_until(
        lambda: stats.snapshot()["failed"] >= 3, within=5.0, reason="three refused publishes"
    )
    logger.remove(sink_id)

    seen = stats.snapshot()
    assert seen["published"] == 0
    assert seen["last_error"] == "ConnectionError"
    assert seen["pending"] == 0


async def test_an_accepted_publish_is_counted_and_nothing_is_lost(isolated_logger):
    nc = RecordingNats()
    stats = NatsSinkStats()
    sink_id = _add(nc, stats=stats)

    for _ in range(3):
        logger.bind(service="sink_probe").info("a line")
    await wait_until(
        lambda: stats.snapshot()["published"] >= 3, within=5.0, reason="three accepted publishes"
    )
    logger.remove(sink_id)

    seen = stats.snapshot()
    assert (seen["failed"], seen["dropped"], seen["pending"], seen["last_error"]) == (0, 0, 0, None)


def test_a_loop_that_has_closed_drops_the_record_and_leaves_no_coroutine_behind(isolated_logger):
    stats = NatsSinkStats()

    async def attach() -> None:
        _add(RecordingNats(), stats=stats)
        logger.complete()
        # The sink's own announcement is published before the loop closes: a record
        # that arrives mid-shutdown is a different race from the one under test.
        await wait_until(
            lambda: stats.snapshot()["published"] >= 1,
            within=5.0,
            reason="the sink's announcement was published",
        )

    asyncio.run(attach())  # the loop the sink captured is closed from here on
    before = stats.snapshot()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(2):
            logger.bind(service="sink_probe").info("after the loop closed")
        logger.complete()
        gc.collect()

    seen = stats.snapshot()
    assert seen["dropped"] - before["dropped"] == 2
    assert seen["last_error"] is not None
    assert seen["pending"] == 0, "a dropped record must not hold a slot"
    assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []


async def test_a_loop_that_closes_between_the_check_and_the_schedule_leaves_no_coroutine(
    isolated_logger,
):
    nc = RecordingNats()
    stats = NatsSinkStats()
    sink_id = _add(nc, stats=stats)
    logger.complete()
    await asyncio.sleep(0)  # let the sink's own announcement run first
    await wait_until(
        lambda: stats.snapshot()["pending"] == 0, within=5.0, reason="the announcement published"
    )
    before = stats.snapshot()
    loop = asyncio.get_running_loop()

    def closed_loop(*args, **kwargs):
        raise RuntimeError("Event loop is closed")

    loop.call_soon_threadsafe = closed_loop  # type: ignore[method-assign]
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            logger.bind(service="sink_probe").info("a line")
            logger.complete()
            gc.collect()
    finally:
        del loop.call_soon_threadsafe  # back to the class's method
        logger.remove(sink_id)

    seen = stats.snapshot()
    assert seen["dropped"] - before["dropped"] == 1
    assert seen["last_error"] == "RuntimeError"
    assert seen["pending"] == 0, "a record that was never scheduled must not hold a slot"
    assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []


async def test_a_failing_sink_writes_one_line_to_stderr_not_one_per_record(isolated_logger, capsys):
    stats = NatsSinkStats()
    sink_id = _add(DownNats(), stats=stats)

    for _ in range(5):
        logger.bind(service="sink_probe").info("a line")
    logger.complete()
    await wait_until(
        lambda: stats.snapshot()["failed"] >= 5, within=5.0, reason="five refused publishes"
    )
    logger.remove(sink_id)

    lines = [ln for ln in capsys.readouterr().err.splitlines() if "publish failed" in ln]
    assert len(lines) == 1, lines
    assert "ConnectionError" in lines[0]


async def test_a_publish_that_succeeds_makes_the_next_failure_visible_again(
    isolated_logger, capsys
):
    nc = FlakyNats()
    stats = NatsSinkStats()
    sink_id = _add(nc, stats=stats)
    await wait_until(
        lambda: stats.snapshot()["failed"] >= 1, within=5.0, reason="the first refusal"
    )

    nc.down = False
    logger.bind(service="sink_probe").info("a line that gets through")
    logger.complete()
    await wait_until(
        lambda: stats.snapshot()["published"] >= 1, within=5.0, reason="a publish succeeded"
    )
    nc.down = True
    logger.bind(service="sink_probe").info("a line that fails again")
    logger.complete()
    await wait_until(
        lambda: stats.snapshot()["failed"] >= 2, within=5.0, reason="the second refusal"
    )
    logger.remove(sink_id)

    lines = [ln for ln in capsys.readouterr().err.splitlines() if "publish failed" in ln]
    assert len(lines) == 2, lines


async def test_the_backlog_is_bounded_and_the_overflow_is_counted(isolated_logger):
    nc = BlockedNats()
    stats = NatsSinkStats()
    sink_id = _add(nc, stats=stats, max_pending=10)

    for _ in range(50):
        logger.bind(service="sink_probe").info("a line")
    await _drained(stats, lines=50)

    held = stats.snapshot()
    assert held["pending"] == 10
    assert held["dropped"] >= 40  # the sink's own announcement may add one more
    assert held["last_error"] == "BacklogFull"
    await wait_until(lambda: nc.started == 10, within=5.0, reason="ten publishes started")

    nc.release.set()
    await wait_until(
        lambda: stats.snapshot()["pending"] == 0, within=5.0, reason="the backlog drained"
    )
    logger.remove(sink_id)
    assert stats.snapshot()["published"] == 10
    assert nc.started == 10, "a dropped record must never reach the connection"


async def test_a_publish_cancelled_before_it_finishes_is_counted_as_dropped_and_frees_its_slot(
    isolated_logger,
):
    nc = BlockedNats()
    stats = NatsSinkStats()
    sink_id = _add(nc, stats=stats)
    for _ in range(3):
        logger.bind(service="sink_probe").info("a line")
    logger.complete()
    await wait_until(
        lambda: nc.started >= 3, within=5.0, reason="three publishes are waiting on the broker"
    )
    before = stats.snapshot()

    for task in asyncio.all_tasks() - {asyncio.current_task()}:
        task.cancel()
    await wait_until(
        lambda: stats.snapshot()["pending"] == 0, within=5.0, reason="the cancelled slots freed"
    )
    logger.remove(sink_id)

    seen = stats.snapshot()
    assert seen["dropped"] - before["dropped"] == before["pending"]
    assert seen["last_error"] == "Cancelled"
    assert seen["published"] == 0


async def test_CONTROL_below_the_bound_nothing_is_dropped(isolated_logger):
    nc = BlockedNats()
    stats = NatsSinkStats()
    sink_id = _add(nc, stats=stats, max_pending=10)

    for _ in range(5):
        logger.bind(service="sink_probe").info("a line")
    await _drained(stats, lines=5)
    nc.release.set()
    await wait_until(
        lambda: stats.snapshot()["published"] >= 5, within=5.0, reason="all five published"
    )
    logger.remove(sink_id)

    assert stats.snapshot()["dropped"] == 0


async def test_a_record_the_redactor_cannot_handle_is_counted_as_failed(isolated_logger):
    nc = RecordingNats()
    stats = NatsSinkStats()

    def redactor(record: dict) -> dict:
        raise ValueError("cannot redact")

    sink_id = _add(nc, stats=stats, redactor=redactor)
    logger.bind(service="sink_probe").info("a line")
    logger.complete()
    await wait_until(lambda: stats.snapshot()["failed"] >= 1, within=5.0, reason="a failed record")
    logger.remove(sink_id)

    seen = stats.snapshot()
    assert seen["last_error"] == "ValueError"
    assert nc.payloads == []


def test_a_bound_below_one_is_refused(isolated_logger):
    async def attach() -> None:
        _add(RecordingNats(), stats=NatsSinkStats(), max_pending=0)

    with pytest.raises(ValueError, match="max_pending"):
        asyncio.run(attach())


async def test_the_extension_reports_the_sinks_figures_in_health(isolated_logger):
    class Svc(CliffracerService):
        logging = LoggingExtension(to_nats=True)

    svc = Svc(ServiceConfig(name="health_probe", health_port=0))
    svc.nc = DownNats()
    await svc.container._setup_extensions()
    await svc.logging.start()
    try:
        logger.bind(service="health_probe").info("a line")
        details = svc.logging.health_details()
        assert details is not None
        await wait_until(
            lambda: svc.logging.health_details()["nats_sink"]["failed"] >= 1,
            within=5.0,
            reason="the failed publishes reach /health",
        )
        shown = svc.logging.health_details()["nats_sink"]
        assert shown["last_error"] == "ConnectionError"
        assert shown["published"] == 0
        assert details["streaming"] is True
    finally:
        await svc.logging.stop()


async def test_CONTROL_a_service_that_never_started_the_sink_reports_no_figures(isolated_logger):
    class Svc(CliffracerService):
        logging = LoggingExtension()

    svc = Svc(ServiceConfig(name="quiet_probe", health_port=0))

    assert svc.logging.health_details() == {"to_nats": False, "streaming": False}

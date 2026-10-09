"""The level the NATS sink is given decides which records it publishes.

`add_nats_sink` takes `log_level` and `LoggingExtension(log_level=...)` passes its own through;
every other test of the sink leaves the level at its default, so a sink that ignored it, or an
extension that did not hand it on, published exactly what those tests expect.
"""

import asyncio

import pytest
from cliffracer_logging import LoggingExtension
from cliffracer_logging.config import LoggingConfig
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.discovery import HandlerDiscovery

pytestmark = pytest.mark.unit

SERVICE = "leveled"
CONFIG = ServiceConfig(name=SERVICE, health_port=0)


class RecordingNats:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append(subject)


def _subject(level: str) -> str:
    return HandlerDiscovery.with_namespace(CONFIG, f"logs.{SERVICE}.{level}")


async def _log_one_of_each_and_wait_for_the_last(nc: RecordingNats) -> None:
    """Log at three levels, then wait for the last one to be published.

    The sink hands records to its writer thread in order and publishes them in order, so once the
    error line is out, any lower line the level let through is out before it: what is missing at
    that point was not published.
    """
    log = logger.bind(service=SERVICE)
    log.info("an info line")
    log.warning("a warning line")
    log.error("an error line")
    deadline = asyncio.get_running_loop().time() + 5
    while _subject("error") not in nc.published:
        assert asyncio.get_running_loop().time() < deadline, nc.published
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    ("level", "streamed"),
    [
        ("ERROR", ["error"]),
        ("WARNING", ["warning", "error"]),
        ("INFO", ["info", "info", "warning", "error"]),
    ],
)
async def test_the_sink_publishes_the_records_at_or_above_its_level(level, streamed):
    nc = RecordingNats()
    sink_id = LoggingConfig.add_nats_sink(SERVICE, nc, config=CONFIG, log_level=level)
    try:
        await _log_one_of_each_and_wait_for_the_last(nc)
    finally:
        logger.remove(sink_id)

    # At INFO the sink's own "streaming enabled" line comes first, ahead of the three.
    assert nc.published == [_subject(name) for name in streamed]


async def test_the_extension_hands_its_level_to_the_sink_it_adds():
    class Svc(CliffracerService):
        logging = LoggingExtension(to_nats=True, log_level="ERROR")

    nc = RecordingNats()
    svc = Svc(CONFIG)
    svc.nc = nc
    await svc.container._setup_extensions()
    await svc.logging.start()
    try:
        await _log_one_of_each_and_wait_for_the_last(nc)
    finally:
        await svc.logging.stop()

    assert nc.published == [_subject("error")]

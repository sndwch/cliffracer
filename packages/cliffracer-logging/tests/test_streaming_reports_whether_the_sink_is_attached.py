"""`/health` says `streaming` only while the NATS sink is really in loguru's table.

The extension remembers the id `add_nats_sink` returned. That is not the same
as the sink being attached: `logger.remove()` with no argument detaches every
handler without telling anyone, and this package calls it from
`LoggingConfig.configure` and `setup_correlation_logging`. A second service
configuring logging in the same process silently stopped log streaming while
`/health` kept answering `streaming: true`. The thing that decides is loguru's
handler table, which the other tests in this package already read.
"""

from unittest.mock import AsyncMock

import pytest
from cliffracer_logging import LoggingConfig, LoggingExtension
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


def _attachment(details: dict | None) -> dict:
    """The attachment fields, with the sink's published and lost counts left out."""
    assert details is not None
    return {key: value for key, value in details.items() if key != "nats_sink"}


async def _streaming_service() -> CliffracerService:
    class Svc(CliffracerService):
        logging = LoggingExtension(to_nats=True)

    svc = Svc(ServiceConfig(name="streamer", health_port=0))
    svc.nc = AsyncMock()
    await svc.container._setup_extensions()
    await svc.logging.start()
    return svc


async def test_CONTROL_streaming_is_true_while_the_sink_is_attached():
    svc = await _streaming_service()
    try:
        assert svc.logging._sink_id in logger._core.handlers
        assert _attachment(svc.logging.health_details()) == {"to_nats": True, "streaming": True}
    finally:
        await svc.logging.stop()


async def test_streaming_is_false_once_the_sink_is_removed_behind_the_extensions_back():
    svc = await _streaming_service()
    try:
        logger.remove(svc.logging._sink_id)

        assert _attachment(svc.logging.health_details()) == {"to_nats": True, "streaming": False}
    finally:
        await svc.logging.stop()


async def test_streaming_is_false_after_another_service_configures_logging():
    svc = await _streaming_service()
    sink_id = svc.logging._sink_id
    try:
        LoggingConfig.configure(service_name="other", enable_console=False, enable_file=False)

        assert sink_id not in logger._core.handlers, "the premise: configure() detaches the sink"
        assert _attachment(svc.logging.health_details()) == {"to_nats": True, "streaming": False}
    finally:
        await svc.logging.stop()


async def test_stopping_after_the_sink_was_removed_does_not_raise():
    svc = await _streaming_service()
    logger.remove(svc.logging._sink_id)

    await svc.logging.stop()

    assert _attachment(svc.logging.health_details()) == {"to_nats": True, "streaming": False}

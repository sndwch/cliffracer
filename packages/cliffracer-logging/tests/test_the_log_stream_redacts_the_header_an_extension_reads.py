"""A service's NATS log stream redacts the header its installed extensions read a credential from.

`AuthExtension(header="x-service-token")` reads the caller's credential from a header of the
operator's choosing. The dead-letter publisher already withholds whatever header an installed
extension reads; the log redactor only knew a fixed list of names, so a record carrying that header
under its configured name was published as it was. `LoggingExtension` builds the default redactor
with the headers of the extensions installed on its own service.
"""

import asyncio
import json

import pytest
from cliffracer_logging import LoggingExtension
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


class ReadsAHeader(Extension):
    """Stands in for any extension that reads a credential from a configured header."""

    header = "x-svc-id"


class RecordingNats:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.payloads.append(payload)


def _service(*, installs_header: bool, redactor=None):
    options = {} if redactor is None else {"redactor": redactor}
    if installs_header:

        class WithHeader(CliffracerService):
            logging = LoggingExtension(to_nats=True, **options)
            header_reader = ReadsAHeader()

        return WithHeader(ServiceConfig(name="streamer", health_port=0))

    class WithoutHeader(CliffracerService):
        logging = LoggingExtension(to_nats=True, **options)

    return WithoutHeader(ServiceConfig(name="streamer", health_port=0))


async def _published_extra(*, installs_header: bool, redactor=None) -> dict:
    svc = _service(installs_header=installs_header, redactor=redactor)
    nc = RecordingNats()
    svc.nc = nc  # type: ignore[assignment]
    await svc.container._setup_extensions()
    await svc.logging.start()
    try:
        logger.bind(service="streamer", **{"x-svc-id": "canary-value", "order": "o-1"}).info(
            "a line to stream"
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            for payload in nc.payloads:
                decoded = json.loads(payload)
                if decoded["record"]["message"] == "a line to stream":
                    return decoded["record"]["extra"]
    finally:
        await svc.logging.stop()
    raise AssertionError("the line was not published")


async def test_the_header_an_installed_extension_reads_is_redacted_in_the_stream():
    extra = await _published_extra(installs_header=True)

    assert extra["x-svc-id"] == "[REDACTED]"
    assert extra["order"] == "o-1"


async def test_CONTROL_without_an_extension_reading_it_the_name_is_published_as_it_is():
    extra = await _published_extra(installs_header=False)

    assert extra["x-svc-id"] == "canary-value"


async def test_a_redactor_the_service_supplied_is_used_as_it_is():
    seen: list[dict] = []

    def own(record: dict) -> dict:
        seen.append(record)
        return record

    extra = await _published_extra(installs_header=True, redactor=own)

    assert seen and extra["x-svc-id"] == "canary-value"

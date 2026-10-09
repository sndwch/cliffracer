"""A service name containing braces is data, not a loguru template.

``ServiceConfig`` accepts such a name, so every place the logging package puts it
into a log message, a sink path or a format string has to take it literally.
"""

import pytest
from cliffracer_logging.config import LoggingConfig
from cliffracer_logging.correlation_logging import setup_correlation_logging
from loguru import logger

from cliffracer import ServiceConfig

pytestmark = pytest.mark.unit

NAME = "svc{0}"


@pytest.fixture(autouse=True)
def _clean_loguru():
    logger.remove()
    yield
    logger.remove()


def test_configure_writes_the_log_file_named_after_the_service(tmp_path):
    LoggingConfig.configure(service_name=NAME, log_dir=str(tmp_path), enable_console=False)
    logger.complete()

    assert f"Logging configured for service '{NAME}'" in (tmp_path / f"{NAME}.log").read_text()


def test_configure_human_readable_lines_carry_the_service_name(tmp_path):
    LoggingConfig.configure(
        service_name=NAME, log_dir=str(tmp_path), structured=False, enable_console=False
    )
    logger.complete()

    assert f"| {NAME} | Logging configured" in (tmp_path / f"{NAME}.log").read_text()


# Loguru does not raise when a sink's format is invalid: it writes this banner
# and the record to stderr, and the record carries the message text. Asserting
# the text alone would pass on that dump.
SINK_ERROR = "Logging error in Loguru Handler"


def test_configure_announces_itself_on_the_console_under_the_name(capsys):
    LoggingConfig.configure(service_name=NAME, enable_file=False)
    logger.complete()

    err = capsys.readouterr().err
    assert SINK_ERROR not in err
    assert f"Logging configured for service '{NAME}'" in err


def test_configure_human_readable_console_lines_carry_the_service_name(capsys):
    LoggingConfig.configure(service_name=NAME, enable_file=False, structured=False)
    logger.complete()

    err = capsys.readouterr().err
    assert SINK_ERROR not in err
    assert f"{NAME}\x1b[0m | " in err
    assert "Logging configured" in err


@pytest.mark.asyncio
async def test_add_nats_sink_accepts_the_name():
    class Connection:
        async def publish(self, subject, payload):
            return None

    handler_id = LoggingConfig.add_nats_sink(NAME, Connection(), config=ServiceConfig(name=NAME))

    assert isinstance(handler_id, int)


def test_setup_correlation_logging_writes_files_named_after_the_service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    setup_correlation_logging(NAME, "DEBUG")
    logger.complete()

    assert f"configured for service: {NAME}" in (tmp_path / "logs" / f"{NAME}.log").read_text()
    assert (tmp_path / "logs" / f"{NAME}.json").exists()

"""A line that bound its own `service` keeps it through the correlation-aware sinks.

Core binds `service=<config.name>` on every service logger, and a worker may bind another name.
The sinks `setup_correlation_logging` adds filled in the service name by assignment, so each line
was relabelled with the name the sinks were configured for, whatever the call had bound.
"""

import json

import pytest
from cliffracer_logging import setup_correlation_logging
from loguru import logger

pytestmark = pytest.mark.unit


@pytest.fixture
def sinks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(logger._core.handlers)  # type: ignore[attr-defined]
    setup_correlation_logging("configured-name", "DEBUG", enable_file=True, log_dir=str(tmp_path))
    try:
        yield tmp_path
    finally:
        logger.complete()
        for handler_id in set(logger._core.handlers) - before:  # type: ignore[attr-defined]
            logger.remove(handler_id)


def _json_extra(directory, message):
    lines = (directory / "configured-name.json").read_text().splitlines()
    (record,) = [r["record"] for r in map(json.loads, lines) if r["record"]["message"] == message]
    return record["extra"]


def test_a_service_name_the_call_bound_is_what_the_text_and_json_files_carry(sinks):
    logger.bind(service="ingest").info("from the ingest worker")
    logger.complete()

    assert _json_extra(sinks, "from the ingest worker")["service"] == "ingest"
    text = (sinks / "configured-name.log").read_text()
    line = next(line for line in text.splitlines() if "from the ingest worker" in line)
    assert "| ingest |" in line
    assert "configured-name" not in line


def test_a_line_that_bound_none_carries_the_name_the_sinks_were_configured_for(sinks):
    logger.info("from nowhere in particular")
    logger.complete()

    assert _json_extra(sinks, "from nowhere in particular")["service"] == "configured-name"
    text = (sinks / "configured-name.log").read_text()
    line = next(line for line in text.splitlines() if "from nowhere in particular" in line)
    assert "| configured-name |" in line


def test_the_correlation_id_is_still_added_to_a_line_that_bound_its_service(sinks):
    from cliffracer import set_correlation_id

    set_correlation_id("cid-keeps-working")
    logger.bind(service="ingest").info("with an id")
    logger.complete()

    extra = _json_extra(sinks, "with an id")
    assert (extra["service"], extra["correlation_id"]) == ("ingest", "cid-keeps-working")

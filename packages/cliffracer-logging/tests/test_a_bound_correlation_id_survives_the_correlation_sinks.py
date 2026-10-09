"""A line that bound its own `correlation_id` keeps it through the correlation-aware sinks.

`attach_correlation_id` adds the ambient id "unless the call bound one", and the sinks
`setup_correlation_logging` adds filled the id in by assignment: a line written with
`get_correlation_logger(...).bind(correlation_id=...)` or `get_service_logger(...).with_context(
correlation_id=...)` printed `no-correlation` outside a request and the ambient id inside one.
"""

import json

import pytest
from cliffracer_logging import get_correlation_logger, get_service_logger, setup_correlation_logging
from loguru import logger

from cliffracer import CorrelationContext, set_correlation_id

pytestmark = pytest.mark.unit


@pytest.fixture
def sinks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(logger._core.handlers)  # type: ignore[attr-defined]
    setup_correlation_logging("configured-name", "DEBUG", enable_file=True, log_dir=str(tmp_path))
    try:
        yield tmp_path
    finally:
        CorrelationContext.clear()
        logger.complete()
        for handler_id in set(logger._core.handlers) - before:  # type: ignore[attr-defined]
            logger.remove(handler_id)


def _id_in_json(directory, message):
    lines = (directory / "configured-name.json").read_text().splitlines()
    (record,) = [r["record"] for r in map(json.loads, lines) if r["record"]["message"] == message]
    return record["extra"]["correlation_id"]


def _id_in_text(directory, message):
    text = (directory / "configured-name.log").read_text()
    line = next(line for line in text.splitlines() if message in line)
    return line.split(" | ")[3]


def test_an_id_the_call_bound_outside_a_request_is_what_the_files_carry(sinks):
    get_correlation_logger("m").bind(correlation_id="bound-by-caller").info("outside a request")
    logger.complete()

    assert _id_in_json(sinks, "outside a request") == "bound-by-caller"
    assert _id_in_text(sinks, "outside a request") == "bound-by-caller"


def test_an_id_the_call_bound_inside_a_request_wins_over_the_ambient_one(sinks):
    set_correlation_id("ambient-1")
    get_correlation_logger("m").bind(correlation_id="bound-by-caller").info("inside a request")
    logger.complete()

    assert _id_in_json(sinks, "inside a request") == "bound-by-caller"


def test_an_id_a_service_logger_context_carries_wins_too(sinks):
    set_correlation_id("ambient-2")
    get_service_logger("svc").with_context(correlation_id="ctx-bound").info("via the context")
    logger.complete()

    assert _id_in_json(sinks, "via the context") == "ctx-bound"


def test_a_plain_loguru_line_that_bound_an_id_keeps_it(sinks):
    logger.bind(correlation_id="plain-bound").info("plain loguru")
    logger.complete()

    assert _id_in_json(sinks, "plain loguru") == "plain-bound"


def test_CONTROL_the_ambient_id_is_still_added_to_a_line_that_bound_none(sinks):
    set_correlation_id("ambient-3")
    logger.info("ambient only")
    logger.complete()

    assert _id_in_json(sinks, "ambient only") == "ambient-3"


def test_CONTROL_a_line_with_no_id_anywhere_is_marked_no_correlation(sinks):
    logger.info("nothing bound")
    logger.complete()

    assert _id_in_json(sinks, "nothing bound") == "no-correlation"

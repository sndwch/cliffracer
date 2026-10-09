"""A line written through `ContextualLogger`, `log_rpc_calls` or `log_event_handling` reports a caller's location.

The formats `LoggingConfig.configure` prints carry `{name}:{function}:{line}` for the call site, and
the JSON records carry `name`, `function` and `line`. Every line written through the contextual
logger reported `cliffracer_logging.config:info:448` instead, the wrapper's own frame.
"""

import json

import pytest
from cliffracer_logging import get_service_logger, log_event_handling, log_rpc_calls
from loguru import logger

pytestmark = pytest.mark.unit


@pytest.fixture
def records():
    seen: list[dict] = []
    handler_id = logger.add(lambda m: seen.append(m.record), level="DEBUG")
    try:
        yield seen
    finally:
        logger.remove(handler_id)


def _site(records, message):
    (record,) = [r for r in records if r["message"] == message]
    return record["name"], record["function"], record["line"]


def test_a_contextual_logger_line_reports_the_function_that_wrote_it(records):
    log = get_service_logger("svc")

    def the_function_that_logs():
        log.info("written by the function")  # the line the record must name

    the_function_that_logs()
    name, function, line = _site(records, "written by the function")

    assert (name, function) == (__name__, "the_function_that_logs")
    assert line > 0 and "written by the function" in open(__file__).read().splitlines()[line - 1]


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
def test_every_level_of_the_contextual_logger_reports_the_caller(records, level):
    log = get_service_logger("svc")
    getattr(log, level)(f"at {level}")

    assert _site(records, f"at {level}")[:2] == (
        __name__,
        "test_every_level_of_the_contextual_logger_reports_the_caller",
    )


def test_exception_reports_the_caller(records):
    log = get_service_logger("svc")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("an exception line")

    assert _site(records, "an exception line")[:2] == (
        __name__,
        "test_exception_reports_the_caller",
    )


def test_the_rpc_decorator_lines_report_the_decorated_function(records):
    def handler():
        return 1

    expected_line = handler.__code__.co_firstlineno
    decorated_handler = log_rpc_calls(get_service_logger("svc"))(handler)

    decorated_handler()

    for message in ("RPC call started: handler", "RPC call completed: handler"):
        name, function, line = _site(records, message)
        assert (name, function) == (__name__, "handler"), message
        assert line == expected_line, (message, line)


def test_the_event_decorator_lines_report_the_decorated_function(records):
    def event_handler():
        raise ValueError("fails")

    expected_line = event_handler.__code__.co_firstlineno
    decorated_event_handler = log_event_handling(get_service_logger("svc"))(event_handler)

    with pytest.raises(ValueError):
        decorated_event_handler()

    name, function, line = _site(records, "Event handling failed: event_handler")
    assert (name, function) == (__name__, "event_handler")
    assert line == expected_line


def test_CONTROL_a_plain_loguru_line_reports_its_caller(records):
    logger.info("plain line")

    assert _site(records, "plain line")[:2] == (
        __name__,
        "test_CONTROL_a_plain_loguru_line_reports_its_caller",
    )


def test_the_json_record_carries_the_callers_name_and_function(tmp_path):
    path = tmp_path / "out.json"
    handler_id = logger.add(path, serialize=True, level="DEBUG")
    try:
        get_service_logger("svc").info("to the json sink")
        logger.complete()
    finally:
        logger.remove(handler_id)

    (record,) = [json.loads(line)["record"] for line in path.read_text().splitlines()]
    assert record["name"] == __name__
    assert record["function"] == "test_the_json_record_carries_the_callers_name_and_function"

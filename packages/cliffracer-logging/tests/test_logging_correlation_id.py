"""The loggers the package hands out attach the current correlation id themselves.

The id has to reach the record whatever sinks are installed, so these tests read
it from a plain sink that has no correlation filter of its own.
"""

import pytest
from cliffracer_logging.config import LoggingConfig, get_service_logger
from cliffracer_logging.correlation_logging import get_correlation_logger, setup_correlation_logging
from loguru import logger

from cliffracer.core.correlation import CorrelationContext

pytestmark = pytest.mark.unit


@pytest.fixture
def extras():
    """The ``extra`` of every record a plain sink receives."""
    logger.remove()
    logger.configure(extra={})
    CorrelationContext.clear()
    seen: list[dict] = []
    logger.add(lambda message: seen.append(dict(message.record["extra"])), level="DEBUG")
    yield seen
    logger.remove()
    logger.configure(extra={})
    CorrelationContext.clear()


def test_get_correlation_logger_attaches_the_current_id(extras):
    CorrelationContext.set("corr_XYZ")

    get_correlation_logger("mymod").info("a line")

    assert extras[-1]["correlation_id"] == "corr_XYZ"
    assert extras[-1]["module"] == "mymod"


def test_get_correlation_logger_follows_the_id_as_it_changes(extras):
    log = get_correlation_logger("mymod")

    CorrelationContext.set("corr_one")
    log.info("first")
    CorrelationContext.set("corr_two")
    log.info("second")

    assert [e["correlation_id"] for e in extras] == ["corr_one", "corr_two"]


def test_the_id_survives_the_helpers_own_sinks_being_replaced(extras, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    setup_correlation_logging("svc_a", "DEBUG")
    LoggingConfig.configure(service_name="svc_a", log_dir=str(tmp_path), enable_console=False)
    sink_seen: list[dict] = []
    logger.add(lambda message: sink_seen.append(dict(message.record["extra"])), level="DEBUG")
    CorrelationContext.set("corr_after_replace")

    get_correlation_logger("mymod").info("a line")

    assert sink_seen[-1]["correlation_id"] == "corr_after_replace"


def test_a_correlation_id_bound_on_the_call_wins(extras):
    CorrelationContext.set("corr_ambient")

    get_correlation_logger("mymod").bind(correlation_id="corr_explicit").info("a line")

    assert extras[-1]["correlation_id"] == "corr_explicit"


def test_no_current_id_leaves_the_record_without_one(extras):
    get_correlation_logger("mymod").info("a line")

    assert "correlation_id" not in extras[-1]


def test_get_service_logger_attaches_the_current_id_and_keeps_its_context(extras):
    CorrelationContext.set("corr_svc")

    get_service_logger("svc_a", user_id="123").info("a line", order="o-1")

    assert extras[-1]["correlation_id"] == "corr_svc"
    assert extras[-1]["user_id"] == "123"
    assert extras[-1]["order"] == "o-1"


def test_a_derived_contextual_logger_attaches_the_current_id_too(extras):
    CorrelationContext.set("corr_child")

    get_service_logger("svc_a").with_context(stage="parse").warning("a line")

    assert extras[-1]["correlation_id"] == "corr_child"
    assert extras[-1]["stage"] == "parse"

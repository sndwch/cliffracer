"""`setup_correlation_logging(replace_existing=False)` adds its sinks next to the ones already installed.

The function began with a bare `logger.remove()`, so a sink the host (or another service) had attached
was dropped, and a caller had no way to say otherwise. `LoggingConfig.configure` takes
`replace_existing`; this function now takes it too, and its default keeps the old behaviour.
"""

import inspect
import json

import pytest
from cliffracer_logging import setup_correlation_logging
from loguru import logger

from cliffracer import CorrelationContext, set_correlation_id

pytestmark = pytest.mark.unit


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A host application's own sink, installed before the call under test."""
    monkeypatch.delenv("CLIFFRACER_LOG_DIR", raising=False)
    monkeypatch.delenv("LOG_DIR", raising=False)
    logger.remove()
    logger.configure(
        extra={}
    )  # a global `service` another test left would win over the one under test
    seen: list[str] = []
    logger.add(lambda message: seen.append(message.record["message"]), level="DEBUG")
    yield seen
    logger.complete()
    logger.remove()
    logger.configure(extra={})
    CorrelationContext.clear()


def _lines_of_the_json_file(tmp_path) -> list[str]:
    logger.complete()
    path = tmp_path / "corr.json"
    return [json.loads(line)["record"]["message"] for line in path.read_text().splitlines()]


def test_replace_existing_false_keeps_the_hosts_sink_and_adds_its_own(host, tmp_path):
    setup_correlation_logging("corr", "DEBUG", log_dir=str(tmp_path), replace_existing=False)

    logger.info("after the call")

    assert "after the call" in host
    assert "after the call" in _lines_of_the_json_file(tmp_path)


def test_replace_existing_false_still_fills_the_correlation_id_in_its_own_sinks(host, tmp_path):
    setup_correlation_logging("corr", "DEBUG", log_dir=str(tmp_path), replace_existing=False)
    set_correlation_id("req-42")

    logger.info("carries an id")
    logger.complete()

    (record,) = [
        json.loads(line)["record"]
        for line in (tmp_path / "corr.json").read_text().splitlines()
        if json.loads(line)["record"]["message"] == "carries an id"
    ]
    assert record["extra"]["correlation_id"] == "req-42"
    assert record["extra"]["service"] == "corr"


def test_a_sink_installed_before_the_call_does_not_see_the_keys_the_correlation_sinks_fill(
    host, tmp_path
):
    """The README and the changelog say so: the keys are filled in registration order."""
    extras: list[dict] = []
    logger.add(lambda message: extras.append(dict(message.record["extra"])), level="DEBUG")

    setup_correlation_logging("corr", "DEBUG", log_dir=str(tmp_path), replace_existing=False)
    logger.info("after the call")

    assert extras[-1] == {}


def test_replace_existing_false_announces_itself_to_the_hosts_sink_too(host, tmp_path):
    setup_correlation_logging("corr", "DEBUG", log_dir=str(tmp_path), replace_existing=False)

    assert any("Correlation-aware logging configured" in line for line in host)


@pytest.mark.parametrize("kwargs", [{}, {"replace_existing": True}], ids=["default", "explicit"])
def test_CONTROL_the_default_still_drops_the_sinks_installed_before_the_call(
    host, tmp_path, kwargs
):
    setup_correlation_logging("corr", "DEBUG", log_dir=str(tmp_path), **kwargs)
    seen_at_the_call = list(host)

    logger.info("after the call")

    assert "after the call" in _lines_of_the_json_file(tmp_path)
    assert "after the call" not in host
    assert host == seen_at_the_call


def test_replace_existing_is_keyword_only_and_defaults_to_true():
    parameter = inspect.signature(setup_correlation_logging).parameters["replace_existing"]

    assert parameter.default is True
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

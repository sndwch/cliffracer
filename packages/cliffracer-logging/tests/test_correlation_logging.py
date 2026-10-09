"""Unit tests for setup_correlation_logging and correlation ID log output."""

import json

import pytest

from cliffracer import CorrelationContext, set_correlation_id

pytestmark = pytest.mark.unit


def _configured_in(tmp_path, monkeypatch):
    """Run `setup_correlation_logging("test_service", "DEBUG")` inside `tmp_path`."""
    from cliffracer_logging import setup_correlation_logging

    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    setup_correlation_logging("test_service", "DEBUG")


def _json_records(tmp_path, message: str) -> list[dict]:
    lines = (tmp_path / "logs" / "test_service.json").read_text().splitlines()
    records = [json.loads(line)["record"] for line in lines]
    return [r for r in records if r["message"] == message]


@pytest.mark.asyncio
async def test_correlation_logging(tmp_path, monkeypatch, capsys):
    """The id and the service name reach all three sinks: the text file, the JSON file and stdout."""
    from loguru import logger

    _configured_in(tmp_path, monkeypatch)
    try:
        set_correlation_id("log_test_789")
        logger.info("Test log message")
        logger.complete()  # the sinks are enqueued: wait for the writer threads

        text = (tmp_path / "logs" / "test_service.log").read_text()
        text_line = next(line for line in text.splitlines() if "Test log message" in line)
        assert "log_test_789" in text_line
        assert "test_service" in text_line

        (record,) = _json_records(tmp_path, "Test log message")
        assert record["extra"]["correlation_id"] == "log_test_789"
        assert record["extra"]["service"] == "test_service"

        out_line = next(
            line for line in capsys.readouterr().out.splitlines() if "Test log message" in line
        )
        assert "log_test_789" in out_line
        assert "test_service" in out_line
    finally:
        CorrelationContext.clear()
        logger.remove()


@pytest.mark.asyncio
async def test_a_record_outside_any_request_is_marked_no_correlation(tmp_path, monkeypatch, capsys):
    """The `or "no-correlation"` default: it is what a line with no ambient id carries."""
    from loguru import logger

    CorrelationContext.clear()
    _configured_in(tmp_path, monkeypatch)
    try:
        logger.info("No request here")
        logger.complete()

        (record,) = _json_records(tmp_path, "No request here")
        assert record["extra"]["correlation_id"] == "no-correlation"
        text = (tmp_path / "logs" / "test_service.log").read_text()
        assert any(
            "no-correlation" in line and "No request here" in line for line in text.splitlines()
        )
    finally:
        logger.remove()

"""The log files land in the log directory, whatever the service is called.

`ServiceConfig` accepts a `/` in a service name, and the file names were built by joining the name
onto the log directory: `/srv/x/evil` wrote `/srv/x/evil.log` outside the directory and `a/b` wrote
`logs/a/b.log` below it. A `CLIFFRACER_LOG_DIR` that is set and empty put the files in the working
directory, where the documented default is `./logs`.
"""

from pathlib import Path

import pytest
from cliffracer_logging import LoggingConfig, setup_correlation_logging
from cliffracer_logging._log_dir import resolve_log_dir
from loguru import logger

from cliffracer import ServiceConfig

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_logger():
    before = set(logger._core.handlers)  # type: ignore[attr-defined]
    yield
    logger.complete()
    for handler_id in set(logger._core.handlers) - before:  # type: ignore[attr-defined]
        logger.remove(handler_id)


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


NAMES = ["{outside}/evil", "a/b", "a\\b", "../up"]


@pytest.mark.parametrize("name", NAMES)
def test_configure_writes_inside_the_log_dir_for_a_name_with_a_separator(tmp_path, name):
    base = tmp_path / "base"
    logs, outside = base / "logs", base / "elsewhere"
    outside.mkdir(parents=True)
    name = name.format(outside=outside)

    LoggingConfig.configure(name, log_dir=str(logs), enable_console=False)
    logger.info("a line")
    logger.complete()

    assert _files(outside) == set()
    assert {p.parent for p in logs.rglob("*") if p.is_file()} == {logs}
    assert not (base / "up.log").exists() and not (tmp_path / "up.log").exists()


@pytest.mark.parametrize("name", NAMES)
def test_setup_correlation_logging_writes_inside_the_log_dir_too(tmp_path, name):
    base = tmp_path / "base"
    logs, outside = base / "logs", base / "elsewhere"
    outside.mkdir(parents=True)
    name = name.format(outside=outside)

    setup_correlation_logging(name, log_dir=str(logs))
    logger.info("a line")
    logger.complete()

    assert _files(outside) == set()
    assert {p.parent for p in logs.rglob("*") if p.is_file()} == {logs}


def test_a_service_config_with_a_slash_in_its_name_is_still_accepted():
    """The name stays a legal label; only the file name is made safe."""
    assert ServiceConfig(name="a/b", health_port=0).name == "a/b"


@pytest.mark.parametrize("name", ["orders", "order-service", "orders.v2", "Orders_2"])
def test_CONTROL_a_name_without_a_separator_is_the_file_name_it_was(tmp_path, name):
    LoggingConfig.configure(name, log_dir=str(tmp_path), enable_console=False)
    logger.info("a line")
    logger.complete()

    assert {f"{name}.log", f"{name}_errors.log"} <= _files(tmp_path)


def test_an_empty_CLIFFRACER_LOG_DIR_is_the_default_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIFFRACER_LOG_DIR", "")

    assert resolve_log_dir(None) == Path("./logs")


def test_an_empty_CLIFFRACER_LOG_DIR_writes_under_logs_not_the_working_directory(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIFFRACER_LOG_DIR", "")

    LoggingConfig.configure("svc", enable_console=False)
    logger.info("a line")
    logger.complete()

    assert {"logs/svc.log", "logs/svc_errors.log"} == _files(tmp_path)


def test_CONTROL_an_unset_variable_and_a_named_one_behave_as_before(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLIFFRACER_LOG_DIR", raising=False)
    assert resolve_log_dir(None) == Path("./logs")

    monkeypatch.setenv("CLIFFRACER_LOG_DIR", str(tmp_path / "chosen"))
    assert resolve_log_dir(None) == tmp_path / "chosen"
    assert resolve_log_dir("explicit") == Path("explicit")


@pytest.mark.parametrize(
    ("name", "stem"),
    [("orders", "orders"), ("a/b", "a_b"), ("a\\b", "a_b"), ("/srv/x/evil", "_srv_x_evil")],
)
def test_the_file_name_form_of_a_service_name(name, stem):
    from cliffracer_logging._log_dir import log_file_stem

    assert log_file_stem(name) == stem

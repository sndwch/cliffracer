"""``LoggingConfig.configure`` and the directory it writes log files to."""

import pytest
from cliffracer_logging.config import LoggingConfig
from loguru import logger

pytestmark = pytest.mark.unit


@pytest.fixture
def sentinel_lines():
    """A sink installed before ``configure`` runs, standing for the process's logging."""
    logger.remove()
    lines: list[str] = []
    logger.add(lambda message: lines.append(message.record["message"]), level="DEBUG")
    yield lines
    logger.remove()


def test_a_nested_log_dir_is_created_and_written_to(tmp_path):
    target = tmp_path / "var" / "log" / "orders"

    LoggingConfig.configure(service_name="svc", log_dir=str(target), enable_console=False)
    logger.complete()

    assert "Logging configured for service 'svc'" in (target / "svc.log").read_text()
    logger.remove()


def test_enable_file_false_creates_no_directory(tmp_path):
    target = tmp_path / "logs"

    LoggingConfig.configure(
        service_name="svc", log_dir=str(target), enable_console=False, enable_file=False
    )

    assert not target.exists()
    logger.remove()


def test_a_log_dir_that_cannot_be_created_raises_and_leaves_logging_armed(tmp_path, sentinel_lines):
    not_a_directory = tmp_path / "occupied"
    not_a_directory.write_text("a file, not a directory")

    with pytest.raises(OSError):
        LoggingConfig.configure(
            service_name="svc", log_dir=str(not_a_directory / "logs"), enable_console=False
        )
    logger.info("after-the-failed-configure")

    assert "after-the-failed-configure" in sentinel_lines

"""The two files `LoggingConfig.configure` writes, and the retention policy it hands them.

`configure` adds an ERROR-only file (`<service>_errors.log`) so an operator can find failures
without reading the main log, and passes `rotation`, `retention` and `compression` to both file
sinks so a long-running service does not fill its disk. Neither was asserted anywhere: the whole
error sink and every one of those arguments could be deleted with the suite green.
"""

import time

import pytest
from cliffracer_logging import LoggingConfig
from loguru import logger

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_logger():
    logger.remove()
    yield
    logger.complete()
    logger.remove()


def test_the_main_file_holds_every_level_and_the_errors_file_only_errors(tmp_path):
    LoggingConfig.configure(
        service_name="svc", log_dir=str(tmp_path), enable_console=False, structured=False
    )
    logger.info("an-informational-line")
    logger.error("an-error-line")
    logger.complete()

    main = (tmp_path / "svc.log").read_text()
    errors = (tmp_path / "svc_errors.log").read_text()
    assert "an-informational-line" in main and "an-error-line" in main
    assert "an-error-line" in errors
    assert "an-informational-line" not in errors
    assert "Logging configured for service" not in errors


def _rotated(tmp_path, prefix: str) -> list[str]:
    return sorted(
        p.name for p in tmp_path.iterdir() if p.name.startswith(prefix) and ".gz" in p.name
    )


def test_rotation_compression_and_retention_reach_both_file_sinks(tmp_path):
    """A tiny rotation size makes every line a rotation, compression turns a rotated file into a
    `.gz`, and a one-second retention removes a rotated file once it is older than that."""
    LoggingConfig.configure(
        service_name="svc",
        log_dir=str(tmp_path),
        enable_console=False,
        structured=False,
        rotation="100 B",
        retention="1 second",
        compression="gz",
    )
    for number in range(6):
        logger.error(f"first-batch-{number}-" + "x" * 80)
    logger.complete()
    first_main, first_errors = _rotated(tmp_path, "svc.2"), _rotated(tmp_path, "svc_errors.2")
    assert first_main and first_errors, sorted(p.name for p in tmp_path.iterdir())

    time.sleep(1.3)  # the first batch's rotated files are now older than the retention
    for number in range(3):
        logger.error(f"second-batch-{number}-" + "y" * 80)
    logger.complete()

    for before, prefix in ((first_main, "svc.2"), (first_errors, "svc_errors.2")):
        after = _rotated(tmp_path, prefix)
        assert after, prefix
        assert not set(before) & set(after), f"{prefix}: files past retention were kept: {before}"

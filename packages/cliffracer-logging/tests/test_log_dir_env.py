"""The log directory is read from CLIFFRACER_LOG_DIR.

Every environment variable this library reads begins CLIFFRACER_, so nothing it
reads can collide with an application's own. This one was `LOG_DIR`, which is a
name an application is entitled to use for itself.
"""

import json
import os

import pytest
from cliffracer_logging import LoggingConfig
from loguru import logger


def _configure_into(tmp_path, monkeypatch, **env):
    """Run configure() with a clean logger and a controlled environment."""
    monkeypatch.chdir(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    before = set(logger._core.handlers)
    try:
        LoggingConfig.configure(service_name="probe", enable_console=False)
        logger.info("a message")
    finally:
        for hid in set(logger._core.handlers) - before:
            logger.remove(hid)


@pytest.mark.unit
def test_CLIFFRACER_LOG_DIR_chooses_the_directory(tmp_path, monkeypatch):
    target = tmp_path / "chosen"
    target.mkdir()

    _configure_into(tmp_path, monkeypatch, CLIFFRACER_LOG_DIR=str(target))

    written = (target / "probe.log").read_text().strip().splitlines()
    assert written, "nothing was written to the directory CLIFFRACER_LOG_DIR names"
    assert json.loads(written[-1])["record"]["message"] == "a message"


@pytest.mark.unit
def test_CONTROL_the_old_unprefixed_name_is_ignored(tmp_path, monkeypatch):
    """Set the OLD name and nothing else: it must not choose the directory.

    Without this, a `LOG_DIR or CLIFFRACER_LOG_DIR` fallback would satisfy the
    case above and quietly keep reading an application's own variable.
    """
    old = tmp_path / "old_name"
    old.mkdir()

    _configure_into(tmp_path, monkeypatch, LOG_DIR=str(old))

    assert not list(old.iterdir()), f"the old name still chose the directory: {list(old.iterdir())}"
    # and the default was used instead
    assert (tmp_path / "logs" / "probe.log").exists(), "the ./logs default was not used"


@pytest.mark.unit
def test_CONTROL_with_neither_set_the_default_is_logs(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIFFRACER_LOG_DIR", raising=False)
    monkeypatch.delenv("LOG_DIR", raising=False)

    _configure_into(tmp_path, monkeypatch)

    assert (tmp_path / "logs" / "probe.log").exists()
    assert "CLIFFRACER_LOG_DIR" not in os.environ

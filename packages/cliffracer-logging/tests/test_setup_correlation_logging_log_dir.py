"""`setup_correlation_logging` writes its files where the package's one log-directory setting says.

`LoggingConfig.configure` reads `CLIFFRACER_LOG_DIR`, and the package's rule is that every
variable it reads begins `CLIFFRACER_`. `setup_correlation_logging` wrote `logs/<service>.log` and
`logs/<service>.json` relative to the current directory and read no variable at all, so the one
knob moved half the package. It now resolves the directory the way `configure` does (the
`log_dir` argument, else `CLIFFRACER_LOG_DIR`, else `./logs`), creates it, can be told to write no
files, and enqueues its sinks as `configure`'s are.
"""

import json

import pytest
from cliffracer_logging import setup_correlation_logging
from loguru import logger

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_logger(monkeypatch):
    monkeypatch.delenv("CLIFFRACER_LOG_DIR", raising=False)
    monkeypatch.delenv("LOG_DIR", raising=False)
    logger.remove()
    yield
    logger.complete()
    logger.remove()


def _log_one_line(**kwargs):
    setup_correlation_logging("corr", "DEBUG", **kwargs)
    logger.info("one line")
    logger.complete()


def _message(path) -> str:
    return json.loads(path.read_text().splitlines()[-1])["record"]["message"]


def test_CLIFFRACER_LOG_DIR_chooses_the_directory_and_it_is_created(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "var" / "log" / "orders"  # nested, and not there yet
    monkeypatch.setenv("CLIFFRACER_LOG_DIR", str(target))

    _log_one_line()

    assert sorted(p.name for p in target.iterdir()) == ["corr.json", "corr.log"]
    assert _message(target / "corr.json") == "one line"
    assert not (tmp_path / "logs").exists(), "the files also went to ./logs"


def test_the_log_dir_argument_beats_the_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIFFRACER_LOG_DIR", str(tmp_path / "from_env"))
    chosen = tmp_path / "from_argument"

    _log_one_line(log_dir=str(chosen))

    assert (chosen / "corr.log").exists()
    assert not (tmp_path / "from_env").exists()


def test_CONTROL_with_neither_set_the_files_go_to_logs_under_the_current_directory(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    _log_one_line()

    assert (tmp_path / "logs" / "corr.log").exists()
    assert (tmp_path / "logs" / "corr.json").exists()


def test_CONTROL_the_old_unprefixed_name_is_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "old_name"))

    _log_one_line()

    assert not (tmp_path / "old_name").exists()
    assert (tmp_path / "logs" / "corr.log").exists()


def test_enable_file_false_writes_no_file_and_creates_no_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    _log_one_line(log_dir=str(tmp_path / "nowhere"), enable_file=False)

    assert not (tmp_path / "nowhere").exists()
    assert not (tmp_path / "logs").exists()
    assert "one line" in capsys.readouterr().out, "the console sink must still work"


def test_a_log_dir_that_cannot_be_created_raises_and_leaves_logging_armed(tmp_path):
    lines: list[str] = []
    logger.add(lambda message: lines.append(message.record["message"]), level="DEBUG")
    occupied = tmp_path / "occupied"
    occupied.write_text("a file, not a directory")

    with pytest.raises(OSError):
        setup_correlation_logging("corr", log_dir=str(occupied / "logs"))
    logger.info("after-the-failed-setup")

    assert "after-the-failed-setup" in lines


def test_every_sink_it_adds_is_enqueued(tmp_path):
    setup_correlation_logging("corr", log_dir=str(tmp_path))

    handlers = list(logger._core.handlers.values())

    assert len(handlers) == 3
    assert all(handler._enqueue for handler in handlers), [h._name for h in handlers]

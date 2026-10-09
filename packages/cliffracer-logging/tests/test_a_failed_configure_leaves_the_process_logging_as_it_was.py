"""A `LoggingConfig.configure` that raises leaves the process's logging exactly as it found it.

`configure` removed every handler and then added its sinks one at a time, so a sink that failed
to open (a log directory that exists but cannot be written, a `rotation`, `retention` or
`compression` that loguru refuses) left the process with no handler at all: the host's own sinks
gone, nothing of the service's installed, and every later line written to nobody. With
`replace_existing=False` the sinks added before the failing one stayed installed.

The sinks are added first now, and the handlers that were there are removed only once every one of
them is in; a failure removes just what this call added.
"""

import os

import pytest
from cliffracer_logging import config as logging_config
from cliffracer_logging.config import LoggingConfig
from loguru import logger

pytestmark = pytest.mark.unit


@pytest.fixture
def host():
    """A host application's logging: a global extra and one sink of its own."""
    logger.remove()
    logger.configure(extra={"app": "myapp"})
    seen: list[str] = []
    sink_id = logger.add(lambda message: seen.append(message.record["message"]), level="DEBUG")
    yield seen, sink_id
    logger.remove()
    logger.configure(extra={})


def _handler_ids() -> set[int]:
    return set(logger._core.handlers)  # type: ignore[attr-defined]


def _global_extra() -> dict:
    return dict(logger._core.extra)  # type: ignore[attr-defined]


BAD_FILE_SINKS = [
    pytest.param({"rotation": "not a rotation"}, id="rotation"),
    pytest.param({"retention": "whenever"}, id="retention"),
    pytest.param({"compression": "rar"}, id="compression"),
]


@pytest.mark.parametrize("replace_existing", [True, False])
@pytest.mark.parametrize("bad", BAD_FILE_SINKS)
def test_a_file_sink_loguru_refuses_leaves_the_handlers_as_they_were(
    host, tmp_path, bad, replace_existing
):
    seen, sink_id = host
    before, extra = _handler_ids(), _global_extra()

    with pytest.raises(ValueError):
        LoggingConfig.configure(
            service_name="svc",
            log_dir=str(tmp_path),
            replace_existing=replace_existing,
            **bad,
        )

    assert _handler_ids() == before == {sink_id}
    assert _global_extra() == extra
    logger.info("after")
    assert "after" in seen


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to a directory with no write bit")
def test_a_log_directory_that_cannot_be_written_leaves_the_handlers_as_they_were(host, tmp_path):
    seen, sink_id = host
    unwritable = tmp_path / "ro"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            LoggingConfig.configure(service_name="svc", log_dir=str(unwritable))
    finally:
        unwritable.chmod(0o700)

    assert _handler_ids() == {sink_id}
    logger.info("after")
    assert "after" in seen


def test_an_interrupt_while_the_sinks_are_added_also_removes_what_was_added(
    host, tmp_path, monkeypatch
):
    """The console sink is in by the time the file sink raises; a KeyboardInterrupt is not an
    Exception, and must not strand the first one."""
    seen, sink_id = host
    real_add = logger.add
    calls = []

    def add_then_interrupt(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_add(*args, **kwargs)

    monkeypatch.setattr(logging_config.logger, "add", add_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        LoggingConfig.configure(service_name="svc", log_dir=str(tmp_path), replace_existing=False)

    assert len(calls) == 2, "the premise: the first sink was added before the interrupt"
    assert _handler_ids() == {sink_id}


def test_CONTROL_a_configure_that_succeeds_replaces_the_hosts_sinks_and_returns_its_own(
    host, tmp_path
):
    seen, sink_id = host

    ids = LoggingConfig.configure(service_name="svc", log_dir=str(tmp_path), enable_console=False)

    assert len(ids) == 2
    assert _handler_ids() == set(ids)
    assert sink_id not in _handler_ids()
    assert _global_extra() == {"app": "myapp", "service": "svc"}


def test_CONTROL_replace_existing_false_keeps_the_hosts_sinks_next_to_its_own(host, tmp_path):
    seen, sink_id = host

    ids = LoggingConfig.configure(
        service_name="svc",
        log_dir=str(tmp_path),
        enable_console=False,
        replace_existing=False,
    )

    assert _handler_ids() == {sink_id, *ids}


def test_CONTROL_a_second_configure_removes_the_first_ones_sinks_and_keeps_its_own(host, tmp_path):
    first = LoggingConfig.configure(
        service_name="one", log_dir=str(tmp_path / "one"), enable_console=False
    )

    second = LoggingConfig.configure(
        service_name="two", log_dir=str(tmp_path / "two"), enable_console=False
    )

    assert _handler_ids() == set(second)
    assert not set(first) & _handler_ids()

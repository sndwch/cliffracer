"""A `setup_correlation_logging` that raises leaves the process's logging exactly as it found it.

The call removed every handler and then added its sinks one at a time, so a log directory that
exists but cannot be written left the process with the host's own sinks gone and one console sink
installed that nothing held an id for (the function returns nothing). `LoggingConfig.configure`
adds first and removes the previous handlers once every sink is in; this call does the same, and a
failure removes just what the call added.
"""

import os
import stat

import pytest
from cliffracer_logging import setup_correlation_logging
from loguru import logger

pytestmark = pytest.mark.unit

needs_a_user_the_directory_mode_binds = pytest.mark.skipif(
    os.geteuid() == 0, reason="root can write a read-only directory"
)


@pytest.fixture
def host():
    """A host application's logging: one sink of its own."""
    logger.remove()
    seen: list[str] = []
    sink_id = logger.add(lambda message: seen.append(message.record["message"]), level="DEBUG")
    yield seen, sink_id
    logger.remove()


def _handler_ids() -> set[int]:
    return set(logger._core.handlers)  # type: ignore[attr-defined]


@pytest.fixture
def unwritable(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    yield directory
    directory.chmod(stat.S_IRWXU)


@needs_a_user_the_directory_mode_binds
@pytest.mark.parametrize("replace_existing", [True, False])
def test_a_directory_that_cannot_be_written_leaves_the_handlers_as_they_were(
    host, unwritable, replace_existing
):
    seen, sink_id = host
    before = _handler_ids()

    with pytest.raises(PermissionError):
        setup_correlation_logging("svc", log_dir=str(unwritable), replace_existing=replace_existing)

    assert _handler_ids() == before == {sink_id}
    logger.info("after the failed call")
    assert "after the failed call" in seen


@pytest.mark.parametrize("replace_existing", [True, False])
def test_a_json_log_file_path_that_is_a_directory_leaves_the_handlers_as_they_were(
    host, tmp_path, replace_existing
):
    """The case that fails for root too, which CI runs as: no permission bits are involved.

    The text sink opens first and the JSON sink then finds a directory where its file belongs.
    """
    seen, sink_id = host
    (tmp_path / "svc.json").mkdir()
    before = _handler_ids()

    with pytest.raises(IsADirectoryError):
        setup_correlation_logging("svc", log_dir=str(tmp_path), replace_existing=replace_existing)

    assert _handler_ids() == before == {sink_id}
    logger.info("after the failed call")
    assert "after the failed call" in seen


def test_CONTROL_a_working_setup_still_replaces_the_hosts_sinks(host, tmp_path):
    seen, sink_id = host

    setup_correlation_logging("svc", log_dir=str(tmp_path), enable_file=False)
    logger.info("after the working call")
    logger.complete()

    assert sink_id not in _handler_ids()
    assert "after the working call" not in seen


def test_CONTROL_a_working_setup_without_replacement_keeps_the_hosts_sinks(host, tmp_path):
    seen, sink_id = host

    setup_correlation_logging(
        "svc", log_dir=str(tmp_path), enable_file=False, replace_existing=False
    )
    logger.info("kept")
    logger.complete()

    assert sink_id in _handler_ids()
    assert "kept" in seen

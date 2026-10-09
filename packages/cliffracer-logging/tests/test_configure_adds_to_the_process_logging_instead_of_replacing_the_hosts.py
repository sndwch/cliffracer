"""`LoggingConfig.configure` merges into the process's logging rather than taking it over.

Loguru's `extra` is one dict for the whole process, and `configure(extra=...)` replaces it.
The host application's own context (app, region, version) has to survive a service
configuring logging, and a caller has to be able to say which sinks are its own: the ids
come back, and `replace_existing=False` leaves what is already installed in place.
"""

import json

import pytest
from cliffracer_logging.config import LoggingConfig
from loguru import logger

pytestmark = pytest.mark.unit


@pytest.fixture
def host():
    """A host application's logging: a global extra and one sink of its own."""
    logger.remove()
    logger.configure(extra={"app": "myapp", "region": "eu"})
    seen: list[dict] = []
    sink_id = logger.add(lambda message: seen.append(dict(message.record["extra"])), level="DEBUG")
    yield SimpleHost(seen, sink_id)
    logger.remove()
    logger.configure(extra={})


class SimpleHost:
    def __init__(self, seen: list[dict], sink_id: int) -> None:
        self.seen = seen
        self.sink_id = sink_id


def _extras_from_a_sink_added_now() -> list[dict]:
    """A sink installed after `configure`, so a default `configure` removing the host's does not matter."""
    seen: list[dict] = []
    logger.add(lambda message: seen.append(dict(message.record["extra"])), level="DEBUG")
    return seen


def _live_handler_ids() -> set[int]:
    return set(logger._core.handlers)  # type: ignore[attr-defined]


def _lines(path) -> list[dict]:
    logger.complete()
    return [json.loads(line)["record"] for line in path.read_text().splitlines()]


def test_the_hosts_global_extra_survives_configure(host, tmp_path):
    LoggingConfig.configure(service_name="svc", log_dir=str(tmp_path), enable_console=False)
    seen = _extras_from_a_sink_added_now()

    logger.info("after")

    assert seen[-1] == {"app": "myapp", "region": "eu", "service": "svc"}


def test_configuring_a_second_service_keeps_the_hosts_extra_and_names_the_last(host, tmp_path):
    LoggingConfig.configure(service_name="one", log_dir=str(tmp_path / "one"), enable_console=False)
    LoggingConfig.configure(service_name="two", log_dir=str(tmp_path / "two"), enable_console=False)

    seen = _extras_from_a_sink_added_now()

    logger.info("after")

    assert seen[-1] == {"app": "myapp", "region": "eu", "service": "two"}


@pytest.mark.parametrize(
    ("console", "file", "expected"),
    [(True, True, 3), (False, True, 2), (True, False, 1), (False, False, 0)],
)
def test_configure_returns_the_ids_of_the_sinks_it_added(host, tmp_path, console, file, expected):
    ids = LoggingConfig.configure(
        service_name="svc",
        log_dir=str(tmp_path),
        enable_console=console,
        enable_file=file,
        replace_existing=False,
    )

    assert len(ids) == expected
    assert set(ids) == _live_handler_ids() - {host.sink_id}


def test_removing_the_returned_ids_leaves_the_hosts_sink(host, tmp_path):
    ids = LoggingConfig.configure(
        service_name="svc", log_dir=str(tmp_path), enable_console=False, replace_existing=False
    )

    for sink_id in ids:
        logger.remove(sink_id)

    assert _live_handler_ids() == {host.sink_id}
    logger.info("still here")
    assert host.seen[-1]["app"] == "myapp"


def test_replace_existing_false_keeps_the_hosts_sink_and_the_first_services_files(host, tmp_path):
    LoggingConfig.configure(service_name="one", log_dir=str(tmp_path / "one"), enable_console=False)
    host_sink = []
    logger.add(lambda message: host_sink.append(message.record["message"]), level="DEBUG")
    LoggingConfig.configure(
        service_name="two",
        log_dir=str(tmp_path / "two"),
        enable_console=False,
        replace_existing=False,
    )

    logger.info("a shared line")

    assert "a shared line" in host_sink
    assert any(r["message"] == "a shared line" for r in _lines(tmp_path / "one" / "one.log"))
    assert any(r["message"] == "a shared line" for r in _lines(tmp_path / "two" / "two.log"))


def test_CONTROL_by_default_configure_still_replaces_every_sink(host, tmp_path):
    first = LoggingConfig.configure(
        service_name="one", log_dir=str(tmp_path / "one"), enable_console=False
    )
    LoggingConfig.configure(service_name="two", log_dir=str(tmp_path / "two"), enable_console=False)

    logger.info("a line after the second service")

    assert not set(first) & _live_handler_ids()
    assert host.sink_id not in _live_handler_ids()
    assert all(
        r["message"] != "a line after the second service"
        for r in _lines(tmp_path / "one" / "one.log")
    )

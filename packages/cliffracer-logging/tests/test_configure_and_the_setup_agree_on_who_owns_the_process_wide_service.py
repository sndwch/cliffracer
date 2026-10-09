"""`configure` and `setup_correlation_logging` follow one rule for the process-wide `service`.

Both take `replace_existing`, with one meaning. A call that replaces the process's sinks owns its
logging, so the process-wide `service` becomes its own. A call that adds next to existing sinks
leaves the one another service named, and logs one WARNING that names the kept and the ignored
service. `setup_correlation_logging` kept and warned; `configure` overwrote silently, so which
service a plain-logger line carried depended on which of the two a service called.
"""

import pytest
from cliffracer_logging import LoggingConfig, setup_correlation_logging
from loguru import logger

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_logger(monkeypatch):
    monkeypatch.delenv("CLIFFRACER_LOG_DIR", raising=False)
    logger.remove()
    logger.configure(extra={})
    yield
    logger.complete()
    logger.remove()
    logger.configure(extra={})


def _start(how: str, name: str, tmp_path, *, replace_existing: bool = True) -> None:
    if how == "setup":
        setup_correlation_logging(
            name, enable_file=False, log_dir=str(tmp_path), replace_existing=replace_existing
        )
    else:
        LoggingConfig.configure(
            name,
            enable_console=False,
            enable_file=False,
            log_dir=str(tmp_path),
            replace_existing=replace_existing,
        )


def _watch() -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    labels: list[str] = []
    logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING", format="{message}")
    logger.add(lambda m: labels.append(str(m.record["extra"].get("service"))), level="DEBUG")
    return warnings, labels


@pytest.mark.parametrize("second", ["setup", "configure"])
@pytest.mark.parametrize("first", ["setup", "configure"])
def test_a_second_service_added_next_to_the_first_keeps_the_first_label_and_warns_once(
    first, second, tmp_path
):
    _start(first, "alpha", tmp_path)
    warnings, labels = _watch()

    _start(second, "beta", tmp_path, replace_existing=False)
    logger.info("plain")
    logger.bind(service="bound").info("bound")
    logger.complete()

    assert labels[-2:] == ["alpha", "bound"], labels
    assert len(warnings) == 1, warnings
    assert "'alpha'" in warnings[0] and "'beta'" in warnings[0], warnings


@pytest.mark.parametrize("second", ["setup", "configure"])
@pytest.mark.parametrize("first", ["setup", "configure"])
def test_a_second_service_that_replaces_the_sinks_owns_the_label_and_warns_of_nothing(
    first, second, tmp_path
):
    _start(first, "alpha", tmp_path)

    _start(second, "beta", tmp_path)
    warnings, labels = _watch()
    logger.info("plain")
    logger.bind(service="bound").info("bound")
    logger.complete()

    assert labels == ["beta", "bound"], labels
    assert warnings == []


@pytest.mark.parametrize("second", ["setup", "configure"])
def test_the_same_service_added_again_next_to_itself_warns_of_nothing(second, tmp_path):
    _start("configure", "alpha", tmp_path)
    warnings, labels = _watch()

    _start(second, "alpha", tmp_path, replace_existing=False)
    logger.info("plain")
    logger.complete()

    assert labels[-1] == "alpha"
    assert warnings == []


def test_configure_alone_next_to_the_hosts_sinks_names_the_service_and_warns_of_nothing(tmp_path):
    warnings, labels = _watch()

    LoggingConfig.configure(
        "alpha", enable_console=False, enable_file=False, replace_existing=False
    )
    logger.info("plain")
    logger.complete()

    assert labels[-1] == "alpha"
    assert warnings == []


def test_a_host_context_in_the_process_wide_extra_survives_both(tmp_path):
    logger.configure(extra={"app": "billing"})
    _start("configure", "alpha", tmp_path)
    _start("configure", "beta", tmp_path, replace_existing=False)
    seen: list[dict] = []
    logger.add(lambda m: seen.append(dict(m.record["extra"])), level="DEBUG")

    logger.info("plain")
    logger.complete()

    assert seen[-1]["app"] == "billing" and seen[-1]["service"] == "alpha"


def test_a_setup_added_next_to_the_hosts_sinks_with_no_earlier_service_leaves_the_stamp_alone(
    tmp_path,
):
    """Only `configure` names the process-wide `service` when none is named; the setup's own sinks
    fill it per record, and a sink the host registered before it does not see it."""
    host: list[object] = []
    logger.add(lambda m: host.append(m.record["extra"].get("service")), level="DEBUG")

    setup_correlation_logging(
        "alpha", enable_file=False, log_dir=str(tmp_path), replace_existing=False
    )
    logger.info("plain")
    logger.complete()

    assert host[-1] is None

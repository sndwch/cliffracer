"""After one service configured the process, a later setup for another names that other service.

`LoggingConfig.configure("first")` stores the service name in the process-wide `extra`, which loguru
merges into every record before any filter runs. `setup_correlation_logging("second")` fills
`service` with `setdefault` so that a service a call bound itself is kept, and it cannot tell that
from the process-wide value, so every line after the second setup was labelled `first`.
"""

import pytest
from cliffracer_logging import LoggingConfig, setup_correlation_logging
from loguru import logger

pytestmark = pytest.mark.unit


@pytest.fixture
def seen(monkeypatch, tmp_path):
    monkeypatch.delenv("CLIFFRACER_LOG_DIR", raising=False)
    monkeypatch.delenv("LOG_DIR", raising=False)
    logger.remove()
    logger.configure(extra={})
    lines: list[str] = []
    yield lines
    logger.complete()
    logger.remove()
    logger.configure(extra={})


def _services_of_the_lines_after(setup, seen: list[str]) -> list[str]:
    setup()
    logger.add(lambda message: seen.append(message.record["extra"].get("service")), level="DEBUG")
    logger.info("after the second setup")
    logger.bind(service="bound").info("a call that names its own service")
    logger.complete()
    return list(seen)


def test_a_second_setup_labels_the_lines_after_it_with_its_own_service(seen, tmp_path):
    LoggingConfig.configure("first", log_dir=str(tmp_path / "first"))

    labels = _services_of_the_lines_after(
        lambda: setup_correlation_logging("second", log_dir=str(tmp_path / "second")), seen
    )

    assert labels == ["second", "bound"], labels


def test_CONTROL_with_no_earlier_configure_the_setup_labels_lines_with_its_service(seen, tmp_path):
    labels = _services_of_the_lines_after(
        lambda: setup_correlation_logging("only", log_dir=str(tmp_path / "only")), seen
    )

    assert labels == ["only", "bound"], labels


def _warnings_naming(services: tuple[str, ...], messages: list[str]) -> list[str]:
    return [m for m in messages if all(name in m for name in services)]


def test_with_replace_existing_false_the_earlier_label_stays_and_one_warning_names_both(
    seen, tmp_path
):
    LoggingConfig.configure("first", log_dir=str(tmp_path / "first"))
    warnings: list[str] = []
    logger.add(
        lambda message: warnings.append(message.record["message"]),
        level="WARNING",
        format="{message}",
    )

    labels = _services_of_the_lines_after(
        lambda: setup_correlation_logging(
            "second", log_dir=str(tmp_path / "second"), replace_existing=False
        ),
        seen,
    )

    assert labels == ["first", "bound"], labels
    logger.complete()
    assert len(warnings) == 1, warnings
    assert _warnings_naming(("first", "second"), warnings) == warnings


def test_a_setup_that_adds_next_to_a_sink_and_names_the_service_already_configured_warns_of_nothing(
    seen, tmp_path
):
    LoggingConfig.configure("same", log_dir=str(tmp_path / "same"))
    warnings: list[str] = []
    logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")

    labels = _services_of_the_lines_after(
        lambda: setup_correlation_logging(
            "same", log_dir=str(tmp_path / "again"), replace_existing=False
        ),
        seen,
    )
    logger.complete()

    assert labels == ["same", "bound"]
    assert warnings == []


def test_CONTROL_a_second_setup_under_the_same_name_keeps_the_label(seen, tmp_path):
    LoggingConfig.configure("same", log_dir=str(tmp_path / "same"))

    labels = _services_of_the_lines_after(
        lambda: setup_correlation_logging("same", log_dir=str(tmp_path / "again")), seen
    )

    assert labels == ["same", "bound"]


def test_CONTROL_with_replace_existing_false_and_no_earlier_service_nothing_is_warned(
    seen, tmp_path
):
    warnings: list[str] = []
    logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")

    labels = _services_of_the_lines_after(
        lambda: setup_correlation_logging(
            "only", log_dir=str(tmp_path / "only"), replace_existing=False
        ),
        seen,
    )
    logger.complete()

    assert labels == ["only", "bound"]
    assert warnings == []


def test_the_host_context_in_the_process_wide_extra_survives_the_second_setup(seen, tmp_path):
    logger.configure(extra={"app": "billing", "region": "eu"})
    LoggingConfig.configure("first", log_dir=str(tmp_path / "first"))
    extras: list[dict] = []

    setup_correlation_logging("second", log_dir=str(tmp_path / "second"))
    logger.add(lambda message: extras.append(dict(message.record["extra"])), level="DEBUG")
    logger.info("after")
    logger.complete()

    assert extras[0]["service"] == "second"
    assert (extras[0]["app"], extras[0]["region"]) == ("billing", "eu")


def test_a_setup_whose_sink_cannot_be_opened_does_not_change_the_process_wide_service(
    seen, tmp_path
):
    """The failure has to come from `logger.add`, after the directory exists and before any sink is in.

    A directory named like the JSON log file makes loguru refuse to open it. A log directory that
    cannot be created fails earlier, at `mkdir`, and would not tell a service set after the sinks
    from one set before them.
    """
    LoggingConfig.configure("first", log_dir=str(tmp_path / "first"))
    log_dir = tmp_path / "second"
    log_dir.mkdir()
    (log_dir / "second.json").mkdir()

    with pytest.raises(OSError):
        setup_correlation_logging("second", log_dir=str(log_dir))

    labels = _services_of_the_lines_after(lambda: None, seen)
    assert labels[0] == "first"

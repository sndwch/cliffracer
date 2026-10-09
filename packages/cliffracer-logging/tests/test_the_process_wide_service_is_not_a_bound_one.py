"""A line nothing bound a service to is not published as the service that was configured last.

`LoggingConfig.configure` stores the service name in loguru's process-wide `extra`, which loguru
merges into every record before a sink's filter runs. The NATS sink took the records whose `service`
was its own, so in a process that configured two services a line the host wrote, with no service
bound, carried the last-configured name and was published as that service's. The stamp is now an
instance of a private `str` subclass that loguru keeps through filters, `enqueue=True` and
`serialize=True`; the sink's filter takes a record only when its `service` is its own name and a call
bound it.
"""

import asyncio
import json
import pickle

import pytest
from cliffracer_logging import LoggingConfig, get_service_logger, setup_correlation_logging
from cliffracer_logging._service_stamp import ProcessService, is_bound_service
from loguru import logger

from cliffracer import ServiceConfig
from cliffracer.testing import wait_until

pytestmark = pytest.mark.unit


class RecordingNats:
    def __init__(self) -> None:
        self.subjects: list[str] = []
        self.messages: list[str] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.subjects.append(subject)
        self.messages.append(payload.decode())


@pytest.fixture(autouse=True)
def isolated_logger(monkeypatch):
    monkeypatch.delenv("CLIFFRACER_LOG_DIR", raising=False)
    logger.remove()
    logger.configure(extra={})
    yield
    logger.complete()
    logger.remove()
    logger.configure(extra={})


def _configure(name: str, *, replace_existing: bool = True, **kwargs) -> None:
    LoggingConfig.configure(
        name, enable_console=False, enable_file=False, replace_existing=replace_existing, **kwargs
    )


def _sink(name: str, nc: RecordingNats) -> int:
    config = ServiceConfig(name=name, subject_prefix=None, health_port=0)
    return LoggingConfig.add_nats_sink(name, nc, config=config)


async def _settled(nc: RecordingNats, marker: str) -> None:
    logger.complete()
    await wait_until(
        lambda: any(marker in m for m in nc.messages), within=5.0, reason=f"{marker} published"
    )
    await asyncio.sleep(0.1)
    logger.complete()


def _published(nc: RecordingNats, marker: str) -> list[str]:
    return [s for s, m in zip(nc.subjects, nc.messages, strict=True) if marker in m]


async def test_two_configured_services_publish_a_host_line_under_neither():
    """The audit's probe: configure("alpha"), configure("beta", replace_existing=False)."""
    alpha, beta = RecordingNats(), RecordingNats()
    _configure("alpha")
    _configure("beta", replace_existing=False)
    _sink("alpha", alpha)
    _sink("beta", beta)

    logger.info("host line, unbound")
    logger.bind(service="alpha").info("alpha line")
    await _settled(alpha, "alpha line")

    assert _published(alpha, "alpha line") == ["logs.alpha.info"]
    assert _published(alpha, "host line") == [], "the host's line was published as alpha's"
    assert _published(beta, "host line") == [], "the host's line was published as beta's"
    assert _published(beta, "alpha line") == []


async def test_a_single_configured_service_does_not_publish_a_host_line():
    nc = RecordingNats()
    _configure("alpha")
    _sink("alpha", nc)

    logger.info("host line, unbound")
    logger.bind(service="alpha").info("bound line")
    await _settled(nc, "bound line")

    assert _published(nc, "bound line") == ["logs.alpha.info"]
    assert _published(nc, "host line") == []


@pytest.mark.parametrize(
    "bind",
    [
        lambda: logger.bind(service="alpha"),
        lambda: logger.bind(service=str(_stamp())),
        lambda: logger.bind(service=f"{_stamp()}"),
        lambda: logger.bind(service="".join(["al", "pha"])),
        lambda: get_service_logger("alpha")._logger,
    ],
    ids=["literal", "str(stamp)", "f-string of the stamp", "built", "get_service_logger"],
)
async def test_a_line_a_call_bound_to_the_service_is_published(bind):
    nc = RecordingNats()
    _configure("alpha")
    _sink("alpha", nc)

    bind().warning("bound line")
    await _settled(nc, "bound line")

    assert _published(nc, "bound line") == ["logs.alpha.warning"]


def _stamp() -> str:
    return logger._core.extra["service"]  # type: ignore[attr-defined]


def test_the_stamp_is_the_marker_and_formats_as_the_name():
    _configure("alpha")

    stamp = _stamp()

    assert isinstance(stamp, ProcessService)
    assert stamp == "alpha" and f"{stamp}" == "alpha" and json.dumps(stamp) == '"alpha"'
    assert not is_bound_service(stamp)
    assert is_bound_service(str(stamp)) and is_bound_service("alpha")
    assert not is_bound_service(None) and not is_bound_service(3)


def test_the_marker_survives_a_pickle():
    stamp = ProcessService("alpha")

    copy = pickle.loads(pickle.dumps(stamp))  # noqa: S301

    assert type(copy) is ProcessService and copy == "alpha"


async def test_the_marker_survives_an_enqueue_sink_and_a_bound_value_stays_plain():
    seen: list[tuple[str, type]] = []
    _configure("alpha")
    logger.add(
        lambda m: seen.append((m.record["message"], type(m.record["extra"]["service"]))),
        enqueue=True,
        level="DEBUG",
    )

    logger.info("unbound")
    logger.bind(service="alpha").info("bound")
    logger.complete()
    await wait_until(lambda: len(seen) >= 2, within=5.0, reason="two lines through the queue")

    assert dict(seen) == {"unbound": ProcessService, "bound": str}


def test_the_json_file_still_carries_the_service_on_a_line_nothing_bound(tmp_path):
    LoggingConfig.configure("alpha", log_dir=str(tmp_path), enable_console=False)

    logger.info("host line, unbound")
    logger.bind(service="alpha").info("bound line")
    logger.complete()

    lines = [json.loads(line) for line in (tmp_path / "alpha.log").read_text().splitlines()]
    services = {
        entry["record"]["message"]: entry["record"]["extra"].get("service") for entry in lines
    }
    assert services["host line, unbound"] == "alpha"
    assert services["bound line"] == "alpha"


async def test_a_correlation_setup_that_takes_over_logging_stamps_the_marker_too():
    nc = RecordingNats()
    setup_correlation_logging("alpha", enable_file=False)
    _sink("alpha", nc)

    logger.info("host line, unbound")
    logger.bind(service="alpha").info("bound line")
    await _settled(nc, "bound line")

    assert isinstance(_stamp(), ProcessService)
    assert _published(nc, "host line") == []
    assert _published(nc, "bound line") == ["logs.alpha.info"]


async def test_a_correlation_setup_alone_does_not_make_a_host_line_the_services():
    nc = RecordingNats()
    setup_correlation_logging("alpha", enable_file=False, replace_existing=False)
    _sink("alpha", nc)

    logger.info("host line, unbound")
    logger.bind(service="alpha").info("bound line")
    await _settled(nc, "bound line")

    assert _published(nc, "host line") == []


def test_CONTROL_a_text_sink_labels_an_unbound_line_with_the_one_process_wide_name(tmp_path):
    """The limit the README states: the process-wide label is one name, the first when one is added next to it."""
    seen: list[str] = []
    _configure("alpha")
    _configure("beta", replace_existing=False)
    logger.add(lambda m: seen.append(m.record["extra"]["service"]), level="DEBUG")

    logger.info("unbound")

    assert seen == ["alpha"]


async def test_a_host_that_stamps_a_plain_service_itself_is_taken_to_have_bound_it():
    """The other stated limit: only the stamp the package writes is a stamp."""
    nc = RecordingNats()
    logger.configure(extra={"service": "alpha"})
    _sink("alpha", nc)

    logger.info("host line with a plain stamp")
    await _settled(nc, "plain stamp")

    assert _published(nc, "plain stamp") == ["logs.alpha.info"]

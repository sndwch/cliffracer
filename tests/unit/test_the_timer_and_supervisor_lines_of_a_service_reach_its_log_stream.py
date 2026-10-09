"""A timer's failure and a supervisor's activation lines are bound to their service, so its log stream carries them.

The NATS log sink streams a record only when a call bound its service. These lines were written
through the bare loguru logger and reached the stream of whichever service was configured last by way
of the process-wide stamp, which is not a binding any more. A timer's lines go through the logger the
timer binds to its service, and the supervisor's through one bound to its host service. Lines with no
service to name (the correlation helpers, the client, a shared limiter, the batch processor, the
orchestrator and the CLI) stay unbound, and the changelog says they are no longer streamed.
"""

import asyncio

import pytest
from cliffracer_logging import LoggingConfig
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer.core.extension import Extension
from cliffracer.testing import wait_until

pytestmark = pytest.mark.unit


class RecordingNats:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.messages.append(f"{subject} {payload.decode()}")


@pytest.fixture(autouse=True)
def isolated_logger(monkeypatch):
    monkeypatch.delenv("CLIFFRACER_LOG_DIR", raising=False)
    logger.remove()
    logger.configure(extra={})
    yield
    logger.complete()
    logger.remove()
    logger.configure(extra={})


def _stream(name: str) -> RecordingNats:
    """`configure` stamps the process-wide service, as a running service does, and a sink streams it."""
    LoggingConfig.configure(
        name, enable_console=False, enable_file=False, replace_existing=False, log_level="DEBUG"
    )
    nc = RecordingNats()
    config = ServiceConfig(name=name, subject_prefix=None, health_port=0)
    LoggingConfig.add_nats_sink(name, nc, config=config, log_level="DEBUG")
    return nc


async def _published(nc: RecordingNats, marker: str) -> list[str]:
    logger.complete()
    await wait_until(
        lambda: (logger.complete(), any(marker in m for m in nc.messages))[1],
        within=5.0,
        reason=f"{marker!r} streamed",
    )
    await asyncio.sleep(0.1)
    logger.complete()
    return [m for m in nc.messages if marker in m]


class Crashing(Extension):
    fails_closed = True

    async def worker_setup(self, ctx):
        raise RuntimeError("the auth backend is down")


class Ticker(CliffracerService):
    @timer(interval=0.01)
    async def tick(self):
        raise RuntimeError("the method broke")


class GatedTicker(CliffracerService):
    gate = Crashing()

    @timer(interval=0.01)
    async def tick(self):
        return None


async def _fire(cls):
    svc = cls(ServiceConfig(name="ticker", subject_prefix=None, health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    t = svc._timers[0]
    t.service_instance = svc
    t.method_name = "tick"
    await t._execute_method()
    return t


async def test_a_timer_methods_failure_is_in_its_services_stream():
    nc = _stream("ticker")
    other = _stream("other")

    await _fire(Ticker)

    lines = await _published(nc, "Error executing timer method tick")
    assert lines and lines[0].startswith("logs.ticker.error "), lines
    assert not [m for m in other.messages if "Error executing timer method" in m]


async def test_a_gate_that_crashed_on_a_firing_is_in_its_services_stream():
    nc = _stream("ticker")

    t = await _fire(GatedTicker)

    lines = await _published(nc, "Error executing timer method tick")
    assert lines and lines[0].startswith("logs.ticker.error "), lines
    assert t.error_count == 1


async def test_a_timer_stopped_from_its_own_handler_says_so_in_its_services_stream():
    nc = _stream("ticker")
    svc = Ticker(ServiceConfig(name="ticker", subject_prefix=None, health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    t = svc._timers[0]
    t.service_instance = svc
    t.method_name = "tick"
    t._is_inside_its_own_run = lambda: True  # type: ignore[method-assign]
    t.is_running = True
    t._stop_event = asyncio.Event()

    await t.stop()

    lines = await _published(nc, "from its own handler")
    assert lines and lines[0].startswith("logs.ticker.info "), lines


async def test_CONTROL_a_line_nothing_bound_a_service_to_is_still_not_streamed():
    nc = _stream("ticker")

    logger.error("a library line with no service to name")
    logger.bind(service="ticker").error("a marker line")
    await _published(nc, "a marker line")

    assert not [m for m in nc.messages if "a library line" in m]

"""A NATS log sink publishes the records bound to its service and no others.

Loguru sinks are process-wide. The sink was added without a filter, so with two streaming services
in one process a line written for `billing` was published under `logs.orders.*` as well as
`logs.billing.*`, and a line from the host application's own code went out under both. Each sink now
takes the records whose `extra["service"]` is its service. A record with no binding is not streamed.
The framework's own lines for a service carry the binding, and these tests read that too, because a
filter on a key nothing sets would silently stream nothing.
"""

import asyncio
from types import SimpleNamespace

import pytest
from cliffracer_logging import LoggingConfig, LoggingExtension, get_service_logger
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import WorkerContext
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
def isolated_logger():
    logger.remove()
    yield
    logger.remove()


def _config(name: str) -> ServiceConfig:
    return ServiceConfig(name=name, subject_prefix=None, health_port=0)


def _sink(name: str, nc: RecordingNats, level: str = "INFO") -> int:
    return LoggingConfig.add_nats_sink(name, nc, config=_config(name), log_level=level)


async def _settled(nc: RecordingNats, marker: str) -> None:
    """Wait until a line carrying `marker` has been published, then give stragglers a moment."""
    logger.complete()
    await wait_until(
        lambda: any(marker in message for message in nc.messages), within=5.0, reason=marker
    )
    await asyncio.sleep(0.1)
    logger.complete()


async def test_two_streaming_services_do_not_publish_each_others_lines():
    nc = RecordingNats()
    _sink("orders", nc)
    _sink("billing", nc)

    logger.bind(service="billing").warning("billing line")
    await _settled(nc, "billing line")

    ours = [s for s, m in zip(nc.subjects, nc.messages, strict=True) if "billing line" in m]
    assert ours == ["logs.billing.warning"], ours


async def test_a_line_from_the_host_application_is_not_streamed_under_any_service():
    nc = RecordingNats()
    _sink("orders", nc)
    _sink("billing", nc)

    logger.bind(service="host-app").error("host line")
    logger.error("unbound host line")
    logger.bind(service="billing").error("marker line")
    await _settled(nc, "marker line")

    assert not [m for m in nc.messages if "host line" in m], nc.messages


async def test_CONTROL_a_service_still_streams_its_own_lines_at_and_above_its_level():
    nc = RecordingNats()
    _sink("orders", nc, level="WARNING")

    logger.bind(service="orders").info("below the level")
    logger.bind(service="orders").error("an error of its own")
    await _settled(nc, "an error of its own")

    assert not [m for m in nc.messages if "below the level" in m]
    assert "logs.orders.error" in nc.subjects


async def test_the_sinks_own_announcement_reaches_the_sink():
    nc = RecordingNats()
    _sink("orders", nc)

    await _settled(nc, "NATS log streaming enabled for service 'orders'")

    assert "logs.orders.info" in nc.subjects


async def test_the_lines_the_framework_writes_for_a_service_reach_its_sink():
    nc = RecordingNats()
    _sink("orders", nc, level="DEBUG")
    service = CliffracerService(_config("orders"))

    service.logger.info("from the service logger")
    get_service_logger("orders").info("from get_service_logger")
    get_service_logger("orders").with_context(order_id="o-1").info("from a contextual logger")
    await _settled(nc, "from a contextual logger")

    for text in ("from the service logger", "from get_service_logger", "from a contextual logger"):
        assert [m for m in nc.messages if text in m], (text, nc.messages)


async def test_the_timing_line_of_the_logging_extension_reaches_its_sink():
    nc = RecordingNats()

    class Svc(CliffracerService):
        logging = LoggingExtension(to_nats=True, log_level="DEBUG")

    svc = Svc(_config("orders"))
    svc.nc = nc
    await svc.container._setup_extensions()
    await svc.logging.start()
    try:
        ctx = WorkerContext(
            kind="rpc", subject="orders.rpc.go", headers={}, correlation_id="c", payload={}
        )
        await svc.logging.worker_setup(ctx)
        await svc.logging.worker_result(ctx, None, None)
        await _settled(nc, "orders.rpc.go")
    finally:
        await svc.logging.stop()

    assert [m for m in nc.messages if "rpc orders.rpc.go" in m], nc.messages


async def test_the_warning_that_nothing_is_streamed_is_bound_to_the_service():
    seen: list[dict] = []
    logger.add(lambda message: seen.append(message.record["extra"]), level="WARNING")

    class Svc(CliffracerService):
        logging = LoggingExtension(to_nats=True)

    svc = Svc(_config("orders"))
    await svc.container._setup_extensions()
    svc.nc = None
    await svc.logging.start()
    logger.complete()

    assert any(extra.get("service") == "orders" for extra in seen), seen


def test_the_timer_binds_its_lines_to_the_service_it_belongs_to():
    from cliffracer.core.timer import Timer

    seen: list[dict] = []
    logger.add(lambda message: seen.append(message.record["extra"]), level="DEBUG")
    timer = Timer(interval=60.0)
    timer.method_name = "tick"
    timer.service_instance = SimpleNamespace(config=SimpleNamespace(name="orders"))

    timer._log.info("a timer line")
    logger.complete()

    assert [extra.get("service") for extra in seen] == ["orders"]


def test_the_health_listener_binds_its_lines_to_its_service():
    from cliffracer.core.health_listener import HealthListener

    seen: list[dict] = []
    logger.add(lambda message: seen.append(message.record["extra"]), level="DEBUG")
    listener = HealthListener(SimpleNamespace(config=SimpleNamespace(name="orders")), "127.0.0.1")

    listener._log.info("a listener line")
    logger.complete()

    assert [extra.get("service") for extra in seen] == ["orders"]


def test_the_broker_probe_binds_its_lines_to_its_service():
    from cliffracer.core.broker_probe import BrokerProbe, ProbeResult

    seen: list[dict] = []
    logger.add(lambda message: seen.append(message.record["extra"]), level="DEBUG")
    probe = BrokerProbe(2.0, 1.0, service="orders")

    probe._say(ProbeResult(ok=False, rtt_ms=None), "was not answered")
    logger.complete()

    assert [extra.get("service") for extra in seen] == ["orders"]


async def test_a_failing_dependency_probe_logs_under_its_service():
    from cliffracer.core.dependencies import Dependency, _run_one

    async def probe() -> dict:
        raise RuntimeError("down")

    seen: list[dict] = []
    logger.add(lambda message: seen.append(message.record["extra"]), level="WARNING")

    await _run_one(Dependency("db", probe), _config("orders"))
    logger.complete()

    assert [extra.get("service") for extra in seen] == ["orders"]

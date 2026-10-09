"""The supervisor's activation lines and a runner's lifecycle lines are bound to the service they are about.

The NATS log sink streams a record only when a call bound its service. These lines were written
through the bare loguru logger and reached a stream only by way of the process-wide stamp, which is
not a binding. The tests run with a stamp for ANOTHER service installed, so a line that is not bound
reads as `elsewhere`, and drive the real paths: an activation that fails to start, a lifecycle
cleanup that fails, a cleanup that does not finish, and a runner whose service crashes, restarts and
loses its broker.
"""

# The fixtures imported below are used by name as test parameters, which ruff reads as redefinitions.
# ruff: noqa: F811

import asyncio

import pytest
from cliffracer_logging import LoggingConfig
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.connection import BrokerConnectionState
from cliffracer.runners import SupervisorLimits
from cliffracer.runners.orchestrator import ServiceRunner
from cliffracer.runners.supervisor import ActivationTerminated
from tests.unit.test_local_supervisor import (  # noqa: F401  (fixtures are used by name)
    ensure,
    shipment_host,
)

pytestmark = pytest.mark.unit

HOST = "shipments"


@pytest.fixture
def lines():
    """Every record written: (message, the `service` it carries, whether a call bound it)."""
    from cliffracer_logging._service_stamp import is_bound_service

    logger.remove()
    logger.configure(extra={})
    LoggingConfig.configure(
        "elsewhere", enable_console=False, enable_file=False, replace_existing=False
    )
    captured: list[tuple[str, object, bool]] = []
    sink = logger.add(
        lambda m: captured.append(
            (
                m.record["message"],
                m.record["extra"].get("service"),
                is_bound_service(m.record["extra"].get("service")),
            )
        ),
        level="DEBUG",
    )
    yield captured
    logger.remove(sink)
    logger.remove()
    logger.configure(extra={})


def _line(lines, text: str):
    found = [entry for entry in lines if text in entry[0]]
    assert found, [entry[0] for entry in lines]
    return found[0]


async def test_an_activation_that_fails_to_start_is_logged_under_the_host_service(
    shipment_host, lines
):
    host, owner, _ = await shipment_host(configure=lambda child: setattr(child, "fail_start", True))
    with pytest.raises(ActivationTerminated):
        await ensure(host, owner)

    _, service, bound = _line(lines, "startup or contract verification failed")

    assert service == HOST and bound


async def test_a_failed_lifecycle_cleanup_is_logged_under_the_host_service(shipment_host, lines):
    gate = asyncio.Event()
    limits = SupervisorLimits(max_active=1, cleanup_timeout=0.02, monitor_interval=0.005)
    host, owner, children = await shipment_host(limits=limits, stop_gate=gate)
    reference = await ensure(host, owner)
    children[0].fail_stop = True
    await host.stop(reference)
    gate.set()
    await asyncio.gather(*host.unfinished_tasks, return_exceptions=True)
    for _ in range(20):
        await asyncio.sleep(0.01)

    _, service, bound = _line(lines, "lifecycle cleanup failed")

    assert service == HOST and bound


async def test_an_unfinished_cleanup_is_logged_under_the_host_service(shipment_host, lines):
    gate = asyncio.Event()
    limits = SupervisorLimits(max_active=1, cleanup_timeout=0.02, monitor_interval=0.005)
    host, owner, _ = await shipment_host(limits=limits, stop_gate=gate)
    reference = await ensure(host, owner)

    await asyncio.wait_for(host.stop(reference), timeout=1)

    _, service, bound = _line(lines, "cleanup did not finish")
    gate.set()
    assert service == HOST and bound


def _config(name: str, **kwargs) -> ServiceConfig:
    return ServiceConfig(
        name=name, health_port=0, health_listener=False, restart_delay=0.01, **kwargs
    )


class CannotStart(CliffracerService):
    async def start(self) -> None:
        raise RuntimeError("cannot reach the broker")


class StartsThenLosesTheBroker(CliffracerService):
    async def start(self) -> None:
        self._closed = True

    async def stop(self) -> None:
        return None

    @property
    def broker_state(self) -> BrokerConnectionState:
        if getattr(self, "_closed", False):
            return BrokerConnectionState.CLOSED
        return BrokerConnectionState.CONNECTED


async def test_a_services_crash_and_its_restart_are_logged_under_the_service(lines):
    runner = ServiceRunner(CannotStart, config=_config("flaky", auto_restart=True))

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(runner.run(), timeout=0.4)

    for text in ("Starting service 'flaky'", "Service crashed", "Restarting service in"):
        _, service, bound = _line(lines, text)
        assert service == "flaky" and bound, text


async def test_a_broker_that_closes_under_a_running_service_is_logged_under_the_service(lines):
    runner = ServiceRunner(StartsThenLosesTheBroker, config=_config("lonely", auto_restart=False))

    await asyncio.wait_for(runner.run(), timeout=10)

    _, service, bound = _line(lines, "NATS connection closed unexpectedly")
    assert service == "lonely" and bound

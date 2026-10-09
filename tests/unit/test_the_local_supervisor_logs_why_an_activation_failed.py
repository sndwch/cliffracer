"""The local supervisor logs why an activation failed, and names the half of the monitor's condition that fired.

A `TemplateError`, a contract mismatch, a factory error and a failure in `start()` all ended as a
fixed reason in the snapshot, and the exception was retrieved so it would not be reported as
unretrieved and then discarded: the supervisor logged nothing, and neither did an abandoned task or a
failed cleanup. The log now carries the activation's name and the exception's type, and no message
text, because inspection excludes raw exception messages: they can hold application settings and
credentials. The monitor's one condition, a lifecycle that is not running or a broker connection that
is closed, gave one reason; with `exit_on_closed=False` the lifecycle is still running and the reason
named a cause that had not happened.
"""

# The fixtures imported below are used by name as test parameters, which ruff reads as redefinitions.
# ruff: noqa: F811

import asyncio

import pytest
from loguru import logger

from cliffracer.runners import SupervisorLimits
from cliffracer.runners.contracts import ActivationState
from cliffracer.runners.supervisor import ActivationTerminated
from tests.unit.test_local_supervisor import (  # noqa: F401  (fixtures are used by name)
    ensure,
    shipment_host,
    wait_for_state,
)

pytestmark = pytest.mark.unit

SECRET = "private-warehouse-credential"


@pytest.fixture
def lines():
    captured: list[tuple[str, str]] = []
    sink = logger.add(
        lambda message: captured.append((message.record["level"].name, str(message))),
        level="DEBUG",
        format="{message}",
    )
    yield captured
    logger.remove(sink)


def _the_supervisors(lines):
    """The lines the supervisor wrote: the lifecycle's own log lines are not its to redact."""
    return [text for _, text in lines if text.startswith("Activation ")]


def _about_failure(lines):
    return [text for level, text in lines if level in {"WARNING", "ERROR", "CRITICAL"}]


async def test_a_failure_in_startup_is_logged_with_the_activation_and_the_exception_type(
    shipment_host, lines
):
    host, owner, children = await shipment_host(
        configure=lambda child: setattr(child, "fail_start", True)
    )
    with pytest.raises(ActivationTerminated) as failed:
        await ensure(host, owner)

    named = [
        text
        for text in _about_failure(lines)
        if "RuntimeError" in text and "startup or contract verification failed" in text
    ]
    assert len(named) == 1, _about_failure(lines)
    assert failed.value.snapshot.reference.address.service in named[0]
    assert all(SECRET not in text for text in _the_supervisors(lines)), (
        "an exception message reached a line the supervisor wrote"
    )


async def test_a_contract_mismatch_is_logged_with_its_type(shipment_host, lines):
    host, owner, _ = await shipment_host(configure=lambda child: setattr(child, "drift", True))
    with pytest.raises(ActivationTerminated):
        await ensure(host, owner)

    named = [
        text
        for text in _about_failure(lines)
        if "startup or contract verification failed (ContractMismatch)" in text
    ]
    assert len(named) == 1, _about_failure(lines)


async def test_a_startup_that_misses_its_budget_is_logged(shipment_host, lines):
    gate = asyncio.Event()
    limits = SupervisorLimits(startup_timeout=0.05, cleanup_timeout=0.2, wait_timeout=2)
    host, owner, _ = await shipment_host(limits=limits, start_gate=gate)
    with pytest.raises(ActivationTerminated):
        await ensure(host, owner)

    assert any("startup deadline expired" in text for text in _about_failure(lines))


async def test_a_failed_lifecycle_cleanup_is_logged_once_with_its_type(shipment_host, lines):
    gate = asyncio.Event()
    limits = SupervisorLimits(max_active=1, cleanup_timeout=0.02, monitor_interval=0.005)
    host, owner, children = await shipment_host(limits=limits, stop_gate=gate)
    reference = await ensure(host, owner)
    children[0].fail_stop = True
    await host.stop(reference)
    gate.set()
    await asyncio.gather(*host.unfinished_tasks, return_exceptions=True)
    for _ in range(20):
        await asyncio.sleep(0.01)  # the monitor visits the record again and again

    failed = [text for text in _about_failure(lines) if "lifecycle cleanup failed" in text]
    assert len(failed) == 1, failed
    assert "RuntimeError" in failed[0]
    assert all(SECRET not in text for text in _the_supervisors(lines))


async def test_a_lifecycle_cleanup_that_fails_inside_its_budget_is_logged_once_with_its_type(
    shipment_host, lines
):
    limits = SupervisorLimits(max_active=1, cleanup_timeout=0.2, monitor_interval=0.005)
    host, owner, children = await shipment_host(limits=limits)
    reference = await ensure(host, owner)
    children[0].fail_stop = True
    assert not (await host.stop(reference)).complete
    for _ in range(20):
        await asyncio.sleep(0.01)  # the monitor visits the record again and again

    failed = [text for text in _about_failure(lines) if "lifecycle cleanup failed" in text]
    assert len(failed) == 1, failed
    assert "RuntimeError" in failed[0]
    assert all(SECRET not in text for text in _the_supervisors(lines))
    assert (await host.inspect(reference.identity)).reason == "lifecycle cleanup failed"


async def test_a_cleanup_that_is_cancelled_is_logged_once_with_cancelled_as_its_cause(
    shipment_host, lines
):
    """A shutdown task that ends cancelled is a failed cleanup whose cause reads "cancelled", not
    an exception type: nothing in the supervisor cancels it, so the child's own stop raising
    CancelledError is the way the task ends that way."""
    limits = SupervisorLimits(max_active=1, cleanup_timeout=0.2, monitor_interval=0.005)
    host, owner, children = await shipment_host(limits=limits)
    reference = await ensure(host, owner)

    async def cancels_itself() -> None:
        raise asyncio.CancelledError

    real_stop = children[0].stop
    children[0].stop = cancels_itself  # type: ignore[method-assign]
    try:
        assert not (await host.stop(reference)).complete
        for _ in range(20):
            await asyncio.sleep(0.01)
    finally:
        # The child's own stop never ran, so run it now, or the fixture waits on its tasks.
        children[0].stop = real_stop  # type: ignore[method-assign]
        await asyncio.wait_for(real_stop(), timeout=5)

    failed = [text for text in _about_failure(lines) if "lifecycle cleanup failed" in text]
    assert len(failed) == 1, failed
    assert "(cancelled)" in failed[0], failed[0]
    assert (await host.inspect(reference.identity)).reason == "lifecycle cleanup failed"


async def test_an_unfinished_cleanup_is_logged_with_the_tasks_left_running(shipment_host, lines):
    gate = asyncio.Event()
    limits = SupervisorLimits(max_active=1, cleanup_timeout=0.02, monitor_interval=0.005)
    host, owner, _ = await shipment_host(limits=limits, stop_gate=gate)
    reference = await ensure(host, owner)

    outcome = await asyncio.wait_for(host.stop(reference), timeout=1)

    assert not outcome.complete
    unfinished = [text for text in _about_failure(lines) if "cleanup did not finish" in text]
    assert len(unfinished) == 1, _about_failure(lines)
    assert "task(s) still running are abandoned" in unfinished[0]
    gate.set()


async def test_a_closed_broker_connection_under_a_running_lifecycle_says_so(shipment_host, lines):
    limits = SupervisorLimits(
        startup_timeout=1, cleanup_timeout=0.2, wait_timeout=2, monitor_interval=0.005
    )
    host, owner, children = await shipment_host(limits=limits)
    reference = await ensure(host, owner)
    child = children[0]
    assert child.container.is_running
    child.nc.is_closed = True  # nats-py has closed the connection for good; the lifecycle runs on

    snapshot = await wait_for_state(host, reference.identity, ActivationState.FAILED)

    assert snapshot.reason == "child broker connection closed"
    assert any("child broker connection closed" in text for text in _about_failure(lines))
    assert not any("child lifecycle terminated" in text for text in _about_failure(lines))


async def test_CONTROL_a_lifecycle_that_ended_still_says_the_lifecycle_terminated(
    shipment_host, lines
):
    limits = SupervisorLimits(
        startup_timeout=1, cleanup_timeout=0.2, wait_timeout=2, monitor_interval=0.005
    )
    host, owner, children = await shipment_host(limits=limits)
    reference = await ensure(host, owner)
    await children[0].stop()

    snapshot = await wait_for_state(host, reference.identity, ActivationState.FAILED)

    assert snapshot.reason == "child lifecycle terminated"
    assert any("child lifecycle terminated" in text for text in _about_failure(lines))


async def test_CONTROL_a_clean_stop_logs_no_failure(shipment_host, lines):
    host, owner, _ = await shipment_host()
    reference = await ensure(host, owner)

    outcome = await host.stop(reference)

    assert outcome.complete
    assert not [text for text in _about_failure(lines) if "Activation" in text]

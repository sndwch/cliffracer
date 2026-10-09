"""Condition waits report the business observation that never became true."""

import pytest

from cliffracer.testing import wait_until, waiting

pytestmark = pytest.mark.unit


class WarehouseClock:
    def __init__(self) -> None:
        self.now = 40.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def install_clock(monkeypatch) -> WarehouseClock:
    clock = WarehouseClock()
    monkeypatch.setattr(waiting, "monotonic", clock.monotonic)
    monkeypatch.setattr(waiting, "sleep", clock.sleep)
    return clock


async def test_a_shipment_wait_returns_when_the_readiness_observation_becomes_true(monkeypatch):
    clock = install_clock(monkeypatch)
    observations = iter([False, False, True])

    result = await wait_until(
        lambda: next(observations),
        within=1,
        reason="the shipment worker reports ready",
    )

    assert result is None
    assert clock.sleeps == [0.01, 0.01]


async def test_a_timed_out_shipment_wait_names_the_observation_and_the_budget(monkeypatch):
    clock = install_clock(monkeypatch)

    with pytest.raises(AssertionError) as exc:
        await wait_until(
            lambda: False,
            within=0.025,
            reason="the shipment worker reports ready",
        )

    assert str(exc.value) == (
        "Timed out after 0.025s (budget 0.025s): the shipment worker reports ready"
    )
    assert clock.sleeps == [0.01, 0.01, pytest.approx(0.005)]


async def test_a_shipment_observation_that_finishes_after_the_budget_is_not_a_late_success(
    monkeypatch,
):
    clock = install_clock(monkeypatch)

    def slow_observation() -> bool:
        clock.now += 2
        return True

    with pytest.raises(AssertionError, match="Timed out after 2.000s.*loading completes"):
        await wait_until(
            slow_observation,
            within=1,
            reason="shipment loading completes",
        )


async def test_truth_evaluation_that_finishes_after_the_budget_is_not_a_late_success(
    monkeypatch,
):
    clock = install_clock(monkeypatch)

    class SlowReadiness:
        def __bool__(self) -> bool:
            clock.now += 2
            return True

    with pytest.raises(AssertionError, match="Timed out after 2.000s.*loading completes"):
        await wait_until(
            SlowReadiness,
            within=1,
            reason="shipment loading completes",
        )


@pytest.mark.parametrize("within", [0, -1, float("nan"), float("inf"), True])
async def test_an_invalid_wait_budget_is_refused_before_observing_the_warehouse(
    within, monkeypatch
):
    install_clock(monkeypatch)
    observations = 0

    def observe() -> bool:
        nonlocal observations
        observations += 1
        return True

    with pytest.raises((TypeError, ValueError), match="within"):
        await wait_until(observe, within=within, reason="the warehouse opens")
    assert observations == 0


@pytest.mark.parametrize("reason", ["", "   "])
async def test_a_wait_requires_a_sentence_before_observing_the_warehouse(reason, monkeypatch):
    install_clock(monkeypatch)
    observations = 0

    def observe() -> bool:
        nonlocal observations
        observations += 1
        return True

    with pytest.raises(ValueError, match="reason"):
        await wait_until(observe, within=1, reason=reason)
    assert observations == 0


async def test_a_broken_warehouse_observation_keeps_its_own_failure(monkeypatch):
    install_clock(monkeypatch)
    failure = RuntimeError("inventory reader disconnected")

    def observe() -> bool:
        raise failure

    with pytest.raises(RuntimeError) as exc:
        await wait_until(observe, within=1, reason="inventory reaches the packing station")
    assert exc.value is failure


async def test_an_async_warehouse_observation_is_refused_instead_of_passing_immediately(
    monkeypatch,
):
    install_clock(monkeypatch)

    async def observe() -> bool:
        return False

    with pytest.raises(TypeError, match="condition must be synchronous"):
        await wait_until(observe, within=1, reason="the warehouse opens")


async def test_an_already_ready_warehouse_is_observed_once_without_sleeping(monkeypatch):
    clock = install_clock(monkeypatch)
    observations = 0

    def observe() -> bool:
        nonlocal observations
        observations += 1
        return True

    await wait_until(observe, within=1, reason="the warehouse opens")

    assert observations == 1
    assert clock.sleeps == []

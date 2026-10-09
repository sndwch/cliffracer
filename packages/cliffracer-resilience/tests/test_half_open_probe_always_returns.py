"""Half-open admission never remains owned by work that has ended."""

import asyncio

import pytest
from cliffracer_resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    RpcCircuitOpenError,
)

pytestmark = pytest.mark.unit


def recovering_payments() -> CircuitBreaker:
    breaker = CircuitBreaker(
        "payments",
        CircuitBreakerConfig(
            recovery_timeout=0,
            half_open_max_calls=1,
            monitored_exceptions=(ConnectionError,),
        ),
    )
    breaker.trip()
    assert breaker.state is CircuitState.HALF_OPEN
    return breaker


async def test_an_application_error_returns_the_probe_to_the_recovering_service():
    breaker = recovering_payments()

    with pytest.raises(ValueError, match="invalid invoice"):
        async with breaker:
            raise ValueError("invalid invoice")

    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker._half_open_calls == 0

    async with breaker:
        pass

    assert breaker.state is CircuitState.CLOSED


async def test_cancelling_a_probe_returns_admission_for_the_next_health_check():
    breaker = recovering_payments()
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def probe() -> None:
        async with breaker:
            entered.set()
            await hold.wait()

    task = asyncio.create_task(probe())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker._half_open_calls == 0

    async with breaker:
        pass

    assert breaker.state is CircuitState.CLOSED


@pytest.mark.parametrize(
    "older_outcome",
    [
        pytest.param(None, id="success"),
        pytest.param(ValueError("invalid invoice"), id="application-error"),
        pytest.param(ConnectionError(), id="downstream-failure"),
    ],
)
async def test_an_older_payment_request_cannot_decide_a_later_probe(
    older_outcome: BaseException | None,
):
    breaker = CircuitBreaker(
        "payments",
        CircuitBreakerConfig(
            recovery_timeout=0,
            half_open_max_calls=1,
            monitored_exceptions=(ConnectionError,),
        ),
    )
    older_entered = asyncio.Event()
    release_older = asyncio.Event()
    probe_entered = asyncio.Event()
    release_probe = asyncio.Event()

    async def older_request() -> None:
        async with breaker:
            older_entered.set()
            await release_older.wait()
            if older_outcome is not None:
                raise older_outcome

    async def recovery_probe() -> None:
        async with breaker:
            probe_entered.set()
            await release_probe.wait()

    older = asyncio.create_task(older_request())
    await older_entered.wait()
    breaker.trip()
    probe = asyncio.create_task(recovery_probe())
    await probe_entered.wait()

    release_older.set()
    if older_outcome is None:
        await older
    else:
        with pytest.raises(type(older_outcome)):
            await older

    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker._half_open_calls == 1
    with pytest.raises(RpcCircuitOpenError, match="probe in progress"):
        async with breaker:
            pass

    release_probe.set()
    await probe
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.parametrize(
    "older_outcome",
    ["success", "application-error", "downstream-failure", "cancelled"],
)
async def test_a_probe_from_an_older_recovery_cannot_decide_the_current_recovery(
    older_outcome: str,
):
    breaker = CircuitBreaker(
        "payments",
        CircuitBreakerConfig(
            recovery_timeout=0,
            half_open_max_calls=2,
            monitored_exceptions=(ConnectionError,),
        ),
    )
    breaker.trip()
    tasks: list[asyncio.Task[None]] = []
    release_events: list[asyncio.Event] = []

    async def start_probe(outcome: BaseException | None = None) -> asyncio.Task[None]:
        entered = asyncio.Event()
        release = asyncio.Event()
        release_events.append(release)

        async def probe() -> None:
            async with breaker:
                entered.set()
                await release.wait()
                if outcome is not None:
                    raise outcome

        task = asyncio.create_task(probe())
        tasks.append(task)
        await entered.wait()
        return task

    try:
        if older_outcome == "application-error":
            older_error: BaseException | None = ValueError("invalid invoice")
        elif older_outcome == "downstream-failure":
            older_error = ConnectionError("old recovery failed")
        else:
            older_error = None

        older = await start_probe(older_error)
        failed = await start_probe(ConnectionError("recovery failed"))
        release_events[1].set()
        await asyncio.gather(failed, return_exceptions=True)

        current_one = await start_probe()
        current_two = await start_probe()
        assert breaker._half_open_calls == 2

        if older_outcome == "cancelled":
            older.cancel()
        else:
            release_events[0].set()
        await asyncio.gather(older, return_exceptions=True)

        assert breaker.state is CircuitState.HALF_OPEN
        assert breaker._half_open_calls == 2
        with pytest.raises(RpcCircuitOpenError, match="probe in progress"):
            async with breaker:
                pass

        release_events[2].set()
        release_events[3].set()
        await asyncio.gather(current_one, current_two)
    finally:
        for release in release_events:
            release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

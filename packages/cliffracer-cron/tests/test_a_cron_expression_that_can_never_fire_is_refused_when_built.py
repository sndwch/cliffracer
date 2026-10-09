"""A cron expression that names no date is refused when the timer is built.

`croniter.is_valid` checks the syntax. `"0 0 30 2 *"` is valid syntax and names 30 February, so
construction accepted it, `_next_fire` raised `CroniterBadDateError` on every pass, and the service
started normally with a job that never ran and an `Error in cron loop` line every `error_backoff`
seconds for as long as it lived: a typo in a day or month field disabled a job in silence, which the
documentation says a bad expression cannot do.
"""

import asyncio

import pytest
from cliffracer_cron import CronTimer, cron
from cliffracer_cron.distributed import DistributedCronTimer
from loguru import logger

pytestmark = pytest.mark.unit

NEVER = [
    pytest.param("0 0 30 2 *", id="30-february"),
    pytest.param("0 0 31 4 *", id="31-april"),
    pytest.param("0 0 31 6,9,11 *", id="31-of-three-short-months"),
    pytest.param("0 0 31 2,4 *", id="31-of-two-short-months"),
    pytest.param("0 0 30-31 2 *", id="a-range-past-the-end-of-february"),
]

FIRES = [
    pytest.param("0 0 29 2 *", id="29-february-comes-every-leap-year"),
    pytest.param("0 0 31 4,5 *", id="31-may-exists-though-31-april-does-not"),
    pytest.param("0 0 31 * *", id="the-31st-of-the-months-that-have-one"),
    pytest.param("*/5 * * * *", id="every-five-minutes"),
    pytest.param("@yearly", id="a-named-schedule"),
    pytest.param("0 9 * * mon-fri", id="weekdays"),
]


@pytest.mark.parametrize("expression", NEVER)
def test_a_timer_for_an_expression_with_no_date_is_refused_naming_it(expression):
    with pytest.raises(ValueError, match="no date it can fire on") as refused:
        CronTimer(expression)

    assert expression in str(refused.value)


@pytest.mark.parametrize("expression", NEVER)
def test_the_decorator_refuses_it_where_it_is_applied(expression):
    with pytest.raises(ValueError, match="no date it can fire on"):

        @cron(expression)
        async def job(self):  # pragma: no cover - never built
            ...


@pytest.mark.parametrize("expression", NEVER)
def test_a_distributed_timer_refuses_it_too(expression):
    with pytest.raises(ValueError, match="no date it can fire on"):
        DistributedCronTimer(expression, distributed=True)


@pytest.mark.parametrize("expression", FIRES)
def test_CONTROL_an_expression_with_a_date_is_accepted(expression):
    timer = CronTimer(expression)

    assert timer.expression == expression


def test_CONTROL_a_syntax_error_is_still_the_same_refusal_as_before():
    with pytest.raises(ValueError, match="Invalid cron expression"):
        CronTimer("not a cron expression")


def test_CONTROL_a_bad_timezone_is_still_refused_first():
    with pytest.raises(ValueError, match="Invalid timezone"):
        CronTimer("0 0 30 2 *", tz="Not/AZone")


async def test_no_timer_exists_to_log_an_error_every_backoff():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="ERROR", format="{message}")
    try:
        with pytest.raises(ValueError):
            CronTimer("0 0 30 2 *", error_backoff=0.01)
        await asyncio.sleep(0.1)
    finally:
        logger.remove(sink)

    assert lines == []

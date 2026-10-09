"""The correlation extension scopes a timer firing's id, read without the timer's own clean-up.

`TestTimerScopingOnARealService` checks that the context is empty after a failed firing, which the
timer's own `finally: clear()` satisfies whatever the extension did: with the extension's reset
removed, its setup reading the ambient id, or the extension unbound, those tests stay green. These run a
timer-kind dispatch through the container's hook chain directly, with an id already in the context, so
what is read afterwards is what the extension did.
"""

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.correlation import correlation_id_var
from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit

AMBIENT = "corr_ambient_before_the_firing"


class Svc(CliffracerService):
    pass


def _timer_ctx() -> WorkerContext:
    """What the timer builds for a firing. A cron firing is the same: `CronTimer` is a `Timer`."""
    return WorkerContext(
        kind="timer",
        subject="",
        headers={},
        correlation_id=None,
        payload={},
        data={"handler_name": "tick"},
    )


async def _fire(handler):
    """Run one scheduled-kind dispatch with AMBIENT set, returning (outcome, id after)."""
    svc = Svc(ServiceConfig(name="timers", health_port=0))
    await svc.container._setup_extensions()
    token = correlation_id_var.set(AMBIENT)
    try:
        try:
            outcome: object = await svc.container._run_worker(_timer_ctx(), handler)
        except BaseException as exc:  # noqa: BLE001 - which one is part of what is read
            outcome = exc
        return outcome, correlation_id_var.get()
    finally:
        correlation_id_var.reset(token)


async def test_a_firing_that_succeeds_sees_its_own_id_and_leaves_the_ambient_one():
    seen: list[str | None] = []

    async def tick():
        seen.append(correlation_id_var.get())

    _, after = await _fire(tick)

    assert seen and seen[0] is not None
    assert seen[0] != AMBIENT, "the firing read the ambient id instead of being given its own"
    assert after == AMBIENT, "the id the context held before the firing is what it holds after"


async def test_a_firing_that_raises_sees_its_own_id_and_leaves_the_ambient_one():
    seen: list[str | None] = []

    async def boom():
        seen.append(correlation_id_var.get())
        raise RuntimeError("this firing failed")

    outcome, after = await _fire(boom)

    assert seen and seen[0] is not None and seen[0] != AMBIENT, seen
    assert after == AMBIENT, (
        "a firing that raised left its own id in the context instead of restoring the one it "
        f"found: {after!r}"
    )
    assert isinstance(outcome, RuntimeError), "the handler's own exception is what came out"


async def test_two_firings_one_after_another_each_get_their_own_id():
    seen: list[str | None] = []

    async def tick():
        seen.append(correlation_id_var.get())

    svc = Svc(ServiceConfig(name="timers", health_port=0))
    await svc.container._setup_extensions()
    token = correlation_id_var.set(AMBIENT)
    try:
        await svc.container._run_worker(_timer_ctx(), tick)
        await svc.container._run_worker(_timer_ctx(), tick)
    finally:
        correlation_id_var.reset(token)

    assert len(seen) == 2 and None not in seen
    assert seen[0] != seen[1], seen
    assert AMBIENT not in seen

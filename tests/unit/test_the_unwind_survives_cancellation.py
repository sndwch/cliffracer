"""Every `worker_result` and `worker_teardown` runs, even when the task is cancelled.

`run_worker`'s unwind was two bare `for` loops, and `_guarded_hook` re-raises
`asyncio.CancelledError` on purpose. So a cancel delivered while any
`worker_result` was awaiting -- which is what graceful shutdown does to
in-flight dispatch tasks -- aborted the unwind where it stood: the remaining
extensions' `worker_result` hooks were skipped, and the whole `worker_teardown`
loop never started.

`docs/extensions.md` states the contract as `worker_teardown(ctx)` --
"after `worker_result`, ALWAYS". It was not always.

WHAT LEAKS IS WHAT TEARDOWN RELEASES: the otel extension's server span is never
ended and so never exported, and the correlation and auth extensions never
reset their contextvars. No user error is required -- any extension whose
`worker_result` awaits anything opens the window on every dispatch, and
shutdown cancels by design.

The cancellation is still delivered. It is deferred until the unwind is done,
not swallowed: a caller that cancels still sees `CancelledError`, and a task
that would not stop is a worse failure than one that leaks.
"""

import asyncio

import pytest

from cliffracer.core.dispatch.pipeline import ExtensionPipeline
from cliffracer.core.extension import Extension, WorkerContext

pytestmark = pytest.mark.unit


def _ctx() -> WorkerContext:
    return WorkerContext(kind="rpc", subject="s", headers={}, correlation_id="c", payload={})


class Tracer(Extension):
    """Acquires in setup, releases in teardown -- the otel shape."""

    name = "tracer"

    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def worker_setup(self, ctx):
        self.order.append("span-start")

    async def worker_teardown(self, ctx):
        self.order.append("span-end")


class SlowResult(Extension):
    """Its `worker_result` awaits, which is all it takes to open the window."""

    name = "metrics"

    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def worker_result(self, ctx, result, exc):
        self.order.append("flush-begin")
        await asyncio.sleep(0.2)
        self.order.append("flush-end")

    async def worker_teardown(self, ctx):
        self.order.append("metrics-teardown")


async def _cancel_mid_unwind(pipe: ExtensionPipeline, order: list[str]) -> bool:
    async def handler():
        return "ok"

    task = asyncio.create_task(pipe.run_worker(_ctx(), handler))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return True
    return False


@pytest.mark.asyncio
async def test_every_teardown_runs_when_the_task_is_cancelled_mid_unwind():
    """The headline: what setup acquired is released."""
    order: list[str] = []
    pipe = ExtensionPipeline([Tracer(order), SlowResult(order)])

    await _cancel_mid_unwind(pipe, order)

    assert "span-end" in order, order
    assert "metrics-teardown" in order, order


@pytest.mark.asyncio
async def test_the_remaining_worker_result_hooks_still_run():
    """The unwind does not stop at the hook that was awaiting when the cancel
    arrived: the extensions before it in the chain still get their turn."""
    order: list[str] = []

    class Recorder(Extension):
        name = "recorder"

        async def worker_result(self, ctx, result, exc):
            order.append("recorder-result")

    pipe = ExtensionPipeline([Recorder(), SlowResult(order)])

    await _cancel_mid_unwind(pipe, order)

    assert "recorder-result" in order, order


@pytest.mark.asyncio
async def test_the_cancellation_still_reaches_the_caller():
    """Deferred, not swallowed. A task that cannot be cancelled is a worse
    failure than one that leaks, so this is the control on the fix."""
    order: list[str] = []
    pipe = ExtensionPipeline([Tracer(order), SlowResult(order)])

    cancelled = await _cancel_mid_unwind(pipe, order)

    assert cancelled, "the caller did not see CancelledError"


@pytest.mark.asyncio
async def test_CONTROL_an_uncancelled_dispatch_is_unchanged():
    """The ordinary path must not change: every hook, in order, once."""
    order: list[str] = []
    pipe = ExtensionPipeline([Tracer(order), SlowResult(order)])

    async def handler():
        order.append("handler")
        return "ok"

    result = await pipe.run_worker(_ctx(), handler)

    assert result == "ok"
    assert order == [
        "span-start",
        "handler",
        "flush-begin",
        "flush-end",
        "metrics-teardown",
        "span-end",
    ], order


@pytest.mark.asyncio
async def test_a_second_cancellation_during_the_unwind_does_not_wedge_it():
    """Deferring one cancellation must not make the task unkillable.

    An operator escalating a slow shutdown cancels again. Each remaining hook's
    await then ends early and the walk still finishes, rather than the unwind
    absorbing cancellations forever.
    """
    order: list[str] = []

    class SlowTeardown(Extension):
        name = "slow"

        async def worker_teardown(self, ctx):
            order.append("teardown-begin")
            await asyncio.sleep(0.2)
            order.append("teardown-end")

    pipe = ExtensionPipeline([Tracer(order), SlowTeardown()])

    async def handler():
        return "ok"

    task = asyncio.create_task(pipe.run_worker(_ctx(), handler))
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()  # escalate, mid-unwind

    with pytest.raises(asyncio.CancelledError):
        await task

    # The walk reached the far end despite two cancellations.
    assert "span-end" in order, order


@pytest.mark.asyncio
async def test_CONTROL_an_uncancelled_run_raises_nothing_of_its_own():
    """The deferred re-raise must not fire when nothing was cancelled."""
    order: list[str] = []
    pipe = ExtensionPipeline([Tracer(order)])

    async def handler():
        return "ok"

    assert await pipe.run_worker(_ctx(), handler) == "ok"


@pytest.mark.asyncio
async def test_CONTROL_a_handler_exception_still_propagates_after_the_unwind():
    """The unwind must not swallow the handler's own failure, which is what a
    bare `raise cancelled` in the wrong place would do."""
    order: list[str] = []
    pipe = ExtensionPipeline([Tracer(order)])

    async def handler():
        raise RuntimeError("handler exploded")

    with pytest.raises(RuntimeError, match="handler exploded"):
        await pipe.run_worker(_ctx(), handler)

    assert "span-end" in order, order


class CancelsItself(Extension):
    """A hook that is itself cancelled, with a distinct `CancelledError` so its identity is visible."""

    def __init__(self, name: str, order: list[str], *, in_hook: str = "worker_result") -> None:
        self.name = name
        self.order = order
        self.in_hook = in_hook

    async def worker_result(self, ctx, result, exc):
        self.order.append(f"{self.name}.result")
        if self.in_hook == "worker_result":
            raise asyncio.CancelledError(self.name)

    async def worker_teardown(self, ctx):
        self.order.append(f"{self.name}.teardown")
        if self.in_hook == "worker_teardown":
            raise asyncio.CancelledError(self.name)


async def _handler() -> str:
    return "ok"


@pytest.mark.asyncio
async def test_the_first_cancellation_is_the_one_the_caller_receives():
    """When several unwind hooks are cancelled the caller gets the first, as the comment says.

    `worker_result` hooks run in reverse declaration order, so "late" runs first. Keeping the last
    cancellation instead would hand the caller "early". The send-hook unwind pins the same rule.
    """
    order: list[str] = []
    pipe = ExtensionPipeline([CancelsItself("early", order), CancelsItself("late", order)])

    with pytest.raises(asyncio.CancelledError) as raised:
        await pipe.run_worker(_ctx(), _handler)

    assert order == [
        "late.result",
        "early.result",
        "late.teardown",
        "early.teardown",
    ], "every hook runs, the later-declared first"
    assert raised.value.args == ("late",)


@pytest.mark.asyncio
async def test_the_first_cancellation_across_both_phases_is_the_one_the_caller_receives():
    """A cancel in `worker_result` comes before any in `worker_teardown`, so it is the one raised."""
    order: list[str] = []
    pipe = ExtensionPipeline(
        [
            CancelsItself("teardown-cancels", order, in_hook="worker_teardown"),
            CancelsItself("result-cancels", order, in_hook="worker_result"),
        ]
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        await pipe.run_worker(_ctx(), _handler)

    assert raised.value.args == ("result-cancels",)
    assert "teardown-cancels.teardown" in order, "the teardown phase still ran after the cancel"


@pytest.mark.asyncio
async def test_CONTROL_a_single_cancellation_is_raised_as_it_was():
    order: list[str] = []
    pipe = ExtensionPipeline([CancelsItself("only", order)])

    with pytest.raises(asyncio.CancelledError) as raised:
        await pipe.run_worker(_ctx(), _handler)

    assert raised.value.args == ("only",)

"""Every `after_call` runs, even when the task is cancelled while one of them awaits.

`docs/extensions.md` says `after_call` "always runs including when the send raised". `run_worker`'s
unwind was made to keep the same promise for `worker_result` and `worker_teardown`
(`test_the_unwind_survives_cancellation.py`); `run_send_hooks` had the bare loop that fix replaced.
`_guarded_hook` re-raises `CancelledError` on purpose, so a cancel that landed while an `after_call`
awaited ended the loop there and every extension declared before it never got its `after_call`:
a tracing extension left its outbound span open and its context token attached.

The cancellation is still delivered. It is deferred until every hook has run, not swallowed.
"""

import asyncio

import pytest

from cliffracer.core.dispatch.pipeline import ExtensionPipeline
from cliffracer.core.extension import Extension, WorkerContext

pytestmark = pytest.mark.unit


def _ctx() -> WorkerContext:
    return WorkerContext(kind="call_rpc", subject="s", headers={}, correlation_id="c", payload={})


class Span(Extension):
    """Acquires in `before_call`, releases in `after_call`: the otel shape."""

    name = "span"

    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def before_call(self, ctx):
        self.order.append("span-start")

    async def after_call(self, ctx, result, exc):
        self.order.append("span-end")


class SlowAfter(Extension):
    """Its `after_call` awaits, which is all it takes to open the window."""

    def __init__(self, label: str, order: list[str], reached: asyncio.Event, hold: float = 30.0):
        self.name = label
        self.order = order
        self.reached = reached
        self.hold = hold

    async def after_call(self, ctx, result, exc):
        self.order.append(f"{self.name}-begin")
        self.reached.set()
        await asyncio.sleep(self.hold)
        self.order.append(f"{self.name}-end")


async def _send_then_cancel_when(pipe: ExtensionPipeline, reached: asyncio.Event) -> bool:
    async def send():
        return "ok"

    task = asyncio.create_task(pipe.run_send_hooks(_ctx(), send))
    await asyncio.wait_for(reached.wait(), 5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return True
    return False


async def test_every_after_call_runs_when_the_task_is_cancelled_mid_unwind():
    order: list[str] = []
    reached = asyncio.Event()
    pipe = ExtensionPipeline([Span(order), SlowAfter("flush", order, reached)])

    await _send_then_cancel_when(pipe, reached)

    assert "span-end" in order, order


async def test_the_after_call_hooks_declared_before_the_awaiting_one_still_run():
    order: list[str] = []
    reached = asyncio.Event()

    class Recorder(Extension):
        name = "recorder"

        async def after_call(self, ctx, result, exc):
            order.append("recorder-after")

    pipe = ExtensionPipeline([Recorder(), SlowAfter("flush", order, reached)])

    await _send_then_cancel_when(pipe, reached)

    assert order == ["flush-begin", "recorder-after"], order


async def test_the_cancellation_still_reaches_the_caller():
    order: list[str] = []
    reached = asyncio.Event()
    pipe = ExtensionPipeline([Span(order), SlowAfter("flush", order, reached)])

    assert await _send_then_cancel_when(pipe, reached), "the caller did not see CancelledError"


async def test_a_second_cancellation_during_the_unwind_does_not_wedge_it():
    """Deferring one cancellation must not make the task unkillable."""
    order: list[str] = []
    first, second = asyncio.Event(), asyncio.Event()
    pipe = ExtensionPipeline(
        [Span(order), SlowAfter("b", order, second), SlowAfter("a", order, first)]
    )

    async def send():
        return "ok"

    task = asyncio.create_task(pipe.run_send_hooks(_ctx(), send))
    await asyncio.wait_for(first.wait(), 5)
    task.cancel()  # lands in a's after_call
    await asyncio.wait_for(second.wait(), 5)
    task.cancel()  # escalate: lands in b's after_call, mid-unwind

    with pytest.raises(asyncio.CancelledError):
        await task

    assert order == ["span-start", "a-begin", "b-begin", "span-end"], order


class CancelsItself(Extension):
    """Its `after_call` ends in a `CancelledError` of its own, named so a caller can tell which."""

    def __init__(self, label: str, order: list[str]) -> None:
        self.name = label
        self.order = order

    async def after_call(self, ctx, result, exc):
        self.order.append(self.name)
        raise asyncio.CancelledError(self.name)


async def test_the_first_cancellation_is_the_one_the_caller_receives():
    """When several hooks are cancelled the caller gets the first, as the comment on the loop says.

    `after_call` hooks run in reverse declaration order, so "late" runs first. Keeping the last
    cancellation instead would hand the caller "early".
    """
    order: list[str] = []
    pipe = ExtensionPipeline([CancelsItself("early", order), CancelsItself("late", order)])

    async def send():
        return "ok"

    with pytest.raises(asyncio.CancelledError) as raised:
        await pipe.run_send_hooks(_ctx(), send)

    assert order == ["late", "early"], "every hook runs, the later-declared first"
    assert raised.value.args == ("late",)


async def test_CONTROL_an_uncancelled_send_runs_every_hook_in_order_and_returns():
    order: list[str] = []
    pipe = ExtensionPipeline([Span(order), SlowAfter("flush", order, asyncio.Event(), hold=0.01)])

    async def send():
        order.append("send")
        return "ok"

    assert await pipe.run_send_hooks(_ctx(), send) == "ok"
    assert order == ["span-start", "send", "flush-begin", "flush-end", "span-end"], order


async def test_CONTROL_a_send_that_raises_still_raises_after_its_after_call_hooks():
    order: list[str] = []
    pipe = ExtensionPipeline([Span(order)])

    async def send():
        raise RuntimeError("broker said no")

    with pytest.raises(RuntimeError, match="broker said no"):
        await pipe.run_send_hooks(_ctx(), send)

    assert order == ["span-start", "span-end"], order


async def test_CONTROL_a_cancel_during_the_send_still_runs_every_after_call():
    order: list[str] = []
    in_send = asyncio.Event()
    pipe = ExtensionPipeline([Span(order)])

    async def send():
        in_send.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(pipe.run_send_hooks(_ctx(), send))
    await asyncio.wait_for(in_send.wait(), 5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert order == ["span-start", "span-end"], order

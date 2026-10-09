"""Synchronous service hosts that do not join cancellation forever.

The standard Runner supplies coroutine execution, context and interrupt handling.
This module owns teardown instead of calling Runner.close(), whose task join has
no deadline. The bound requires an event loop that can still make progress; it
cannot interrupt blocking Python code or terminate executor threads.
"""

from __future__ import annotations

import asyncio
import inspect
import weakref
from collections.abc import Callable, Coroutine
from typing import Any

from loguru import logger

_ABANDONED: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()


def abandon(task: asyncio.Task[Any]) -> None:
    """Record a task whose cancellation grace was exhausted and reported."""
    _ABANDONED.add(task)


def is_abandoned(task: asyncio.Task[Any]) -> bool:
    """Whether shutdown has already exhausted this task's cancellation grace."""
    return task in _ABANDONED


def run[T](
    main: Coroutine[Any, Any, T],
    *,
    teardown_timeout: float | None | Callable[[], float | None] = 30.0,
) -> T:
    """Run a service, bounding the join of leftover tasks during loop teardown.

    A callable timeout is resolved after execution, when self-configuring services
    have been constructed. None permits an unlimited join. Tasks already reported
    by a service drain are not given another grace. Async generator and executor
    cleanup retain their standard asyncio behavior (including its thread limit).

    `main` is closed if it is never run: when called inside a running loop, or when the
    loop cannot be made. A caller's `run(self.run())` would otherwise leave that coroutine
    unawaited, and Python warns about it at whichever later collection frees it.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        _close_if_never_run(main)
        raise RuntimeError("A synchronous service cannot run inside a running event loop")

    runner = asyncio.Runner()
    try:
        loop = runner.get_loop()
    except BaseException:
        _close_if_never_run(main)
        raise
    try:
        return runner.run(main)
    finally:
        try:
            timeout = teardown_timeout() if callable(teardown_timeout) else teardown_timeout
            loop.run_until_complete(_cancel_remaining(loop, timeout))
            loop.run_until_complete(loop.shutdown_asyncgens())
            # The abstract-loop stub omits the timeout supported since Python 3.12.
            loop.run_until_complete(loop.shutdown_default_executor(timeout=300.0))  # type: ignore[call-arg]
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def _close_if_never_run(main: Coroutine[Any, Any, Any]) -> None:
    """Close a coroutine that was never started; one that ran is its task's to finish."""
    if inspect.iscoroutine(main) and inspect.getcoroutinestate(main) == inspect.CORO_CREATED:
        main.close()


async def _cancel_remaining(loop: asyncio.AbstractEventLoop, timeout: float | None) -> None:
    """Cancel each leftover task once, including tasks spawned during cleanup."""
    current = asyncio.current_task()
    deadline = None if timeout is None else loop.time() + timeout
    cancelled: set[asyncio.Task[Any]] = set()
    while True:
        pending = {t for t in asyncio.all_tasks(loop) if t is not current and not t.done()}
        for task in pending - cancelled:
            task.cancel()
            cancelled.add(task)
        waited = {task for task in pending if not is_abandoned(task)}
        if not waited:
            break
        remaining = None if deadline is None else max(0.0, deadline - loop.time())
        if remaining == 0:
            break
        await asyncio.wait(waited, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)

    for task in cancelled:
        if not task.done():
            logger.error(
                f"Task {task.get_name()!r} did not stop after cancellation; "
                "the service event loop is closing with unfinished work."
            )
        elif not task.cancelled() and task.exception() is not None:
            loop.call_exception_handler(
                {
                    "message": "Unhandled exception during service event loop shutdown",
                    "exception": task.exception(),
                    "task": task,
                }
            )

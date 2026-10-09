"""The service lifecycle and concurrency state machine.

Governs serialized startup, shutdown, abortive startup cleanup, active task
tracking, and in-flight request draining.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from loguru import logger as global_logger

from .exceptions import ServiceLifecycleError
from .loop_host import abandon, is_abandoned
from .service_config import ServiceConfig


@dataclass
class LifecycleHooks:
    """Coordinated async hooks executed during phased startup and teardown."""

    setup_extensions: Callable[[], Awaitable[None]]
    discover_handlers: Callable[[], None]
    connect: Callable[[], Awaitable[None]]
    ensure_streams: Callable[[], Awaitable[None]]
    validate_dlq: Callable[[], None]
    is_jetstream_active: Callable[[], bool]
    on_startup: Callable[[], Awaitable[None]]
    start_extensions: Callable[[], Awaitable[None]]
    start_health_listener: Callable[[], Awaitable[None]]
    start_timers: Callable[[], Awaitable[None]]
    setup_subscriptions: Callable[[], Awaitable[None]]
    stop_timers: Callable[[], Awaitable[None]]
    stop_health_listener: Callable[[], Awaitable[None]]
    cancel_subscriptions: Callable[[], Awaitable[None]]
    on_shutdown: Callable[[], Awaitable[None]]
    stop_extensions: Callable[[], Awaitable[None]]
    disconnect: Callable[[], Awaitable[None]]


#: Seconds `on_shutdown` is given when a stop was cancelled before it got there and
#: `shutdown_timeout` is `None`. That setting means the drain has no deadline; a hook run after a
#: cancel needs one anyway, because the cancel is a request to stop and nothing else can end a stop
#: that waits on a hook that never returns, the closed-connection callback inside nats-py's own close
#: among them. It is also the bound used for a `shutdown_timeout` at or below zero
#: (`bounded_shutdown_timeout`).
ON_SHUTDOWN_CEILING: float = 30.0


def bounded_shutdown_timeout(timeout: float | None, log: Any, where: str) -> float | None:
    """`timeout`, a `shutdown_timeout`, with a value at or below zero read as `ON_SHUTDOWN_CEILING`.

    `ServiceConfig` refuses such a value, so only a config built around validation holds one.
    `None` is the one spelling of "no deadline" and is returned as it is; a value at or below zero
    is bounded, never an endless wait and never no time at all, and is reported once here with a
    warning naming it."""
    if timeout is None or timeout > 0:
        return timeout
    ceiling = ON_SHUTDOWN_CEILING
    log.warning(
        f"{where}: shutdown_timeout is {timeout!r}, which is not a duration; bounding it at "
        f"{ceiling:g} seconds (ON_SHUTDOWN_CEILING). Use None to wait without a deadline"
    )
    return ceiling


#: The lifecycle managers whose teardown the current context is inside, by id.
_STOPPING: ContextVar[frozenset[int]] = ContextVar("cliffracer_stopping", default=frozenset())


async def _wait_or_cancel(tasks: list[asyncio.Task[Any]], timeout: float | None = None) -> None:
    """Wait without joining cancellation; propagate caller cancellation to the work."""
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        raise


class LifecycleManager:
    """Deterministic state machine governing service startup and shutdown.

    Invariants:
    - start() and stop() from different tasks are serialized under ``lock``. The exceptions
      are a stop on the startup task itself, which runs inline because that task holds the
      lock, and the abortive cleanup, which runs in a task of its own while ``start()`` holds it.
    - A ``stop()`` made while a stop is already running on the same task, or from a supervised
      task the running stop is draining, returns at once: the stop it asks for is the one under
      way, and waiting for it would be waiting for itself.
    - Cancels and awaits in-flight startup task if stop() is called during startup.
    - Cleans up partial resources in abortive startup without waiting on the lock it runs under.
    - Supervised background tasks are tracked in ``active_tasks`` and drained on stop.
    """

    def __init__(
        self,
        config: ServiceConfig,
        hooks: LifecycleHooks | None = None,
        logger: Any = None,
    ) -> None:
        self.config = config
        self.hooks = hooks
        self.logger = logger or global_logger.bind(service=config.name)

        self._running: bool = False
        self._starting: bool = False
        self._stopped: bool = False
        self._stop_requests: int = 0
        self._startup_succeeded: bool = False
        self._on_startup_completed: bool = False
        #: Teardown steps that have SUCCEEDED since the last start. A stop that follows a
        #: teardown which failed or was cancelled part-way runs only the steps not in here:
        #: replaying a step that already succeeded (closing an extension's pool twice, say)
        #: is not safe, and `stop()` is not documented as idempotent for extensions.
        self._teardown_done: set[str] = set()
        self._start_task: asyncio.Task[Any] | None = None
        self._lifecycle_lock: asyncio.Lock | None = None
        #: How many teardowns (`stop_internal`) are running now. The context mark alone cannot
        #: say a stop is nested: a task created inside a teardown keeps the mark for life.
        self._teardowns_underway: int = 0
        self._active_tasks: set[asyncio.Task[Any]] = set()
        #: Supervised tasks that are running a `stop()` now. The drain waits for the work in flight,
        #: and a task that is stopping the service is not work in flight: waiting for it is waiting
        #: for the stop that is doing the waiting.
        self._stoppers: set[asyncio.Task[Any]] = set()

    @property
    def lock(self) -> asyncio.Lock:
        """The lifecycle mutex: an `asyncio.Lock`, so NOT reentrant.

        `stop()` recognises a call made from inside its own teardown and returns instead of
        waiting on the lock it holds; code that takes this lock itself must not call `stop()`.
        """
        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        return self._lifecycle_lock

    @property
    def is_running(self) -> bool:
        """Indicates whether the service is actively running and processing messages."""
        return self._running

    @property
    def is_starting(self) -> bool:
        """Indicates whether startup procedure is currently executing."""
        return self._starting

    @property
    def is_stopped(self) -> bool:
        """Indicates whether the service has completed shutdown."""
        return self._stopped

    @property
    def stop_requested(self) -> bool:
        """Indicates whether a shutdown request is pending or in progress."""
        return self._stop_requests > 0

    def _is_interrupted(self) -> bool:
        """Check if startup was interrupted by stop or abort request."""
        return bool(self._stopped or self._stop_requests > 0)

    def _leave_interrupted_startup(self) -> None:
        """End a startup that a stop interrupted, saying so when the stop is already done.

        A stop from inside the startup (an `on_startup` that stops its own service) has run to
        completion by the time the next step is reached, and `start()` used to return normally
        for a service that was stopped, so a caller that did not check `is_stopped` took it for
        up. It raises instead, as the other ordering already does: a stop from another task
        cancels the start, which raises `CancelledError`, and that is left as it is, because a
        caller that cancels `start()` on purpose (a startup timeout) must see its cancellation.
        A stop that has been asked for but has not finished is that second case arriving, and
        returns as before.
        """
        if self._stopped:
            raise ServiceLifecycleError(
                f"Service '{self.config.name}' was stopped while it was starting"
            )

    @property
    def active_tasks(self) -> frozenset[asyncio.Task[Any]]:
        """The supervised background tasks running now, as a snapshot.

        The set behind it is kept in one place, by `spawn_supervised_task` and the
        done-callback it registers, so a caller cannot add to it or remove from it.
        Read it again to see a later state.
        """
        return frozenset(self._active_tasks)

    def spawn_supervised_task(
        self,
        coro: Coroutine[Any, Any, Any] | Awaitable[Any],
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        """Create a tracked task with automatic exception retrieval on completion.

        Invariants:
        - The created task is added to ``active_tasks`` before return.
        - On task completion, the task is discarded and any exception is retrieved.
        """
        task: asyncio.Task[Any] = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        self._active_tasks.add(task)
        task.add_done_callback(self._on_supervised_task_done)
        return task

    def adopt_task(self, task: asyncio.Task[Any]) -> None:
        """Track a task that was created elsewhere, so the drain waits for it and reports it.

        For a task that is already being cancelled and has not finished: a timer's run that
        catches the cancellation and carries on. It is handled from here as a supervised task is:
        removed when it finishes, its failure logged, and at the end of the drain's budget
        cancelled again, named at error level and left tracked.
        """
        if task.done():
            return
        self._active_tasks.add(task)
        task.add_done_callback(self._on_supervised_task_done)

    def _on_supervised_task_done(self, task: asyncio.Task[Any]) -> None:
        self._active_tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            # A cancelled task raises here: stopped, not crashed.
            return
        if exc is not None:
            task_name = task.get_name() if hasattr(task, "get_name") else "unnamed_task"
            # The exception text is an argument, not part of the format string:
            # a brace in it must not be read as a placeholder.
            self.logger.opt(exception=exc).error(
                "Unhandled exception in background task '{}': {}", task_name, exc
            )

    async def drain_active_tasks(self, timeout: float | None = 30.0) -> None:
        """Drain active in-flight supervised tasks within the given timeout deadline.

        A supervised task may spawn another before it finishes, so this drains
        until the set is empty rather than awaiting one snapshot of it. Awaiting
        a snapshot returns while a task spawned during the drain is still
        running, and shutdown continues underneath it.

        The deadline spans the whole drain, not each pass. A ``timeout`` of ``None``
        (``shutdown_timeout=None``) sets no deadline: the drain waits for the tasks to finish
        and cancels nothing. One at or below zero is bounded at ``ON_SHUTDOWN_CEILING``, with a
        warning naming it (``bounded_shutdown_timeout``). At expiry, unfinished
        tasks are cancelled and receive one further timeout to finish cleanup.
        A task that still refuses cancellation is reported and left tracked until
        it actually finishes. The synchronous service host closes its loop without
        waiting for that task again; a caller-owned loop remains the caller's
        responsibility. Cancellation cannot forcibly terminate Python code.
        """
        timeout = bounded_shutdown_timeout(
            timeout, self.logger, f"Draining the tasks of service '{self.config.name}'"
        )
        deadline = None if timeout is None else time.monotonic() + timeout
        # Bounds the yields below. One is enough for a `call_soon` callback; a
        # handful covers a callback that schedules another, and stops this
        # spinning if one never runs at all.
        callback_yields_left = 8

        while True:
            tracked = [
                t for t in self._active_tasks if not is_abandoned(t) and t not in self._stoppers
            ]
            if not tracked:
                return

            pending = [t for t in tracked if not t.done()]
            if not pending:
                # Everything tracked has finished, but `_on_supervised_task_done`
                # removes them from a done-callback, which runs on a later loop
                # iteration. Returning on `done()` alone leaves a finished task
                # listed as active; yield so the callbacks run.
                if callback_yields_left <= 0:
                    self._active_tasks.difference_update(tracked)
                    return
                callback_yields_left -= 1
                await asyncio.sleep(0)
                continue

            if deadline is None:
                await _wait_or_cancel(pending)
                continue

            remaining_time = deadline - time.monotonic()
            if remaining_time > 0:
                await _wait_or_cancel(pending, timeout=remaining_time)
                continue

            remaining = [
                t
                for t in self._active_tasks
                if not t.done() and not is_abandoned(t) and t not in self._stoppers
            ]
            names = ", ".join(sorted(repr(t.get_name()) for t in remaining))
            self.logger.warning(
                f"Shutdown timeout ({timeout}s) exceeded while draining active tasks "
                f"for service '{self.config.name}'. Cancelling {len(remaining)} "
                f"remaining task(s): {names}."
            )
            await self._cancel_active_tasks(timeout)
            return

    async def _cancel_active_tasks(self, timeout: float | None) -> None:
        """Give cancellation cleanup one shared grace, including work it spawns."""
        assert timeout is not None and timeout > 0
        deadline = time.monotonic() + timeout
        cancelled: set[asyncio.Task[Any]] = set()
        while True:
            pending = [
                t
                for t in self._active_tasks
                if not t.done() and not is_abandoned(t) and t not in self._stoppers
            ]
            if not pending:
                return
            for task in pending:
                if task not in cancelled:
                    task.cancel()
                    cancelled.add(task)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await _wait_or_cancel(pending, timeout=remaining)

        for task in pending:
            self.logger.error(
                f"Task {task.get_name()!r} did not stop within {timeout}s of cancellation "
                f"for service '{self.config.name}'; shutdown is continuing with unfinished "
                f"work. The task remains active until it finishes or its event loop closes."
            )
            abandon(task)

    async def start(self) -> None:
        """Execute serialized startup sequence across all subsystems."""
        if self.hooks is None:
            raise RuntimeError("LifecycleManager hooks must be configured before start()")

        if self.stop_requested:
            raise ServiceLifecycleError(
                f"Service '{self.config.name}' cannot start: stop has been requested"
            )

        async with self.lock:
            unfinished = [
                t.get_name() for t in self._active_tasks if not t.done() and is_abandoned(t)
            ]
            if unfinished:
                raise ServiceLifecycleError(
                    f"Service '{self.config.name}' cannot start with unfinished shutdown tasks: "
                    + ", ".join(sorted(unfinished))
                )
            if self.stop_requested:
                raise ServiceLifecycleError(
                    f"Service '{self.config.name}' cannot start: stop has been requested"
                )
            if self._running:
                self.logger.warning(
                    f"Service '{self.config.name}' is already running; start() is a no-op"
                )
                return

            self._starting = True
            self._stopped = False
            self._startup_succeeded = False
            self._on_startup_completed = False
            self._teardown_done = set()
            self._start_task = asyncio.current_task()

            try:
                # 1. Extensions setup
                await self.hooks.setup_extensions()
                if self._is_interrupted():
                    return self._leave_interrupted_startup()

                # 2. Handlers discovery
                self.hooks.discover_handlers()

                # 3. Connect to NATS
                await self.hooks.connect()
                if self._is_interrupted():
                    return self._leave_interrupted_startup()

                # 4. Stream provisioning & DLQ validation
                if self.hooks.is_jetstream_active():
                    await self.hooks.ensure_streams()
                    self.hooks.validate_dlq()
                if self._is_interrupted():
                    return self._leave_interrupted_startup()

                # 5. Service on_startup hook
                await self.hooks.on_startup()
                self._on_startup_completed = True
                if self._is_interrupted():
                    return self._leave_interrupted_startup()

                # 6. Start extensions
                await self.hooks.start_extensions()
                if self._is_interrupted():
                    return self._leave_interrupted_startup()

                # 7. Start health listener
                await self.hooks.start_health_listener()
                if self._is_interrupted():
                    return self._leave_interrupted_startup()

                # 8. Start timers
                await self.hooks.start_timers()
                if self._is_interrupted():
                    return self._leave_interrupted_startup()

                self._running = True

                # 9. Setup subscriptions
                await self.hooks.setup_subscriptions()
                self._startup_succeeded = True

            except BaseException as startup_exc:
                self._running = False
                self._starting = False
                self._start_task = None
                cleanup_task = asyncio.create_task(self.stop_internal(from_abortive_startup=True))
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except (Exception, asyncio.CancelledError):
                        pass
                if not cleanup_task.cancelled():
                    cleanup_exc = cleanup_task.exception()
                    if cleanup_exc is not None:
                        self.logger.error(
                            f"Abortive cleanup encountered error for service '{self.config.name}': {cleanup_exc}"
                        )
                raise startup_exc
            finally:
                self._starting = False
                self._start_task = None

    async def stop(self) -> None:
        """Execute serialized teardown of all service resources and subsystems.

        A call made while a stop is running on this task (a hook that calls `stop()` again), or
        from a supervised task the running stop is draining, returns at once. Calls from other
        tasks wait their turn and find the teardown done. A supervised task that is the FIRST to call
        `stop()` (a "shutdown" handler) is not drained by the stop it is running: it finishes after
        `stop()` returns, rather than the stop waiting `shutdown_timeout` for it and then
        cancelling it.
        """
        current = asyncio.current_task()
        if (id(self) in _STOPPING.get() and self._teardowns_underway > 0) or (
            current is not None and self._stop_requests > 0 and current in self._active_tasks
        ):
            self.logger.debug(
                f"stop() for service '{self.config.name}' was called from inside a stop that is "
                f"already running; nothing to wait for"
            )
            return
        self._stop_requests += 1
        stopper = current if current is not None and current in self._active_tasks else None
        if stopper is not None:
            self._stoppers.add(stopper)
        try:
            if self._starting:
                start_task = self._start_task
                if start_task is not None and start_task is not current:
                    start_task.cancel()
                    # Waited for without awaiting it: the start's own `CancelledError` (or its
                    # failure) is not this stop's to raise, and a cancel aimed at this stop,
                    # which `await start_task` under a blanket handler dropped, still arrives.
                    await asyncio.wait({start_task})
                    if not start_task.cancelled():
                        start_task.exception()

            if self._start_task is current:
                if self._stopped and not self._running:
                    return
                await self.stop_internal(from_abortive_startup=False)
                return

            async with self.lock:
                if self._stopped and not self._running:
                    return
                await self.stop_internal(from_abortive_startup=False)
        finally:
            self._stop_requests = max(0, self._stop_requests - 1)
            if stopper is not None:
                self._stoppers.discard(stopper)

    async def stop_internal(self, *, from_abortive_startup: bool = False) -> None:
        """Run the teardown, marking the context so a `stop()` made inside it is recognised.

        The mark is a context variable and not a task, because the teardown runs its hooks in
        tasks of its own (`asyncio.shield` wraps a coroutine in one), and a task created inside
        it inherits the mark. It inherits it for life, so `stop()` also requires a teardown to
        be running now: a task that outlives one and calls `stop()` after a restart is stopping
        the service, not nesting.
        """
        token = _STOPPING.set(_STOPPING.get() | {id(self)})
        self._teardowns_underway += 1
        try:
            await self._stop_internal(from_abortive_startup=from_abortive_startup)
        finally:
            self._teardowns_underway -= 1
            _STOPPING.reset(token)

    async def _on_shutdown_after_a_cancel(
        self, errors: list[Exception], cancelled: asyncio.CancelledError
    ) -> asyncio.CancelledError:
        """Run `on_shutdown` for a stop that was cancelled before it reached it.

        Shielded: a further cancel of the stop while it runs is remembered and does not abandon
        it, so the extensions are not stopped and the connection is not dropped underneath it.
        Always bounded, so that shielding cannot make a stop that nothing can end: by
        `shutdown_timeout`, or by `ON_SHUTDOWN_CEILING` when that is `None`, which sets no deadline
        for the drain and so cannot be the deadline for a hook run after a cancel, or is at or below
        zero (reported with a warning). Past the bound
        the hook is cancelled, left behind and logged. Returns the cancellation to raise once the
        teardown is over, which is the first one.
        """
        assert self.hooks is not None
        timeout = getattr(self.config, "shutdown_timeout", 30.0)
        bounded = bounded_shutdown_timeout(
            timeout, self.logger, f"on_shutdown of service '{self.config.name}' after a cancel"
        )
        bound = ON_SHUTDOWN_CEILING if bounded is None else bounded
        deadline = time.monotonic() + bound
        task = asyncio.ensure_future(self.hooks.on_shutdown())
        while not task.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                continue
        if not task.done():
            task.cancel()
            # Nobody will await it again; read its outcome so it is not reported as unretrieved.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self.logger.warning(
                f"Service '{self.config.name}' on_shutdown did not finish within {bound:g} "
                f"seconds of its stop being cancelled and was abandoned"
            )
            return cancelled
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.logger.exception(f"Error in on_shutdown for service '{self.config.name}': {exc}")
            errors.append(exc)
        return cancelled

    async def _release_intake_after_a_cancel(self, errors: list[Exception]) -> None:
        """Stop the health listener and cancel the subscriptions of a stop that was cancelled.

        A cancel that lands while the stop waits on the timers, or in either of these two steps,
        jumps past the ones it had not reached, and nothing runs them later because this stop
        marks the service stopped. The service would report stopped with its port still bound
        and its subscriptions still delivering. Both are quick and independent of the timers, so
        they run now, shielded as `on_shutdown` is: a further cancel is remembered, not obeyed,
        and each step is bounded by `shutdown_timeout` (or `ON_SHUTDOWN_CEILING` when that is
        `None`, or at or below zero, which is reported with a warning) so shielding cannot make a
        stop that nothing can end. The drain is not run: a
        cancel asks the stop to stop waiting for work in flight.
        """
        assert self.hooks is not None
        timeout = getattr(self.config, "shutdown_timeout", 30.0)
        bounded = bounded_shutdown_timeout(
            timeout, self.logger, f"Releasing the intake of service '{self.config.name}'"
        )
        bound = ON_SHUTDOWN_CEILING if bounded is None else bounded
        steps = (
            ("stop_health_listener", self.hooks.stop_health_listener, "stopping health listener"),
            ("cancel_subscriptions", self.hooks.cancel_subscriptions, "cancelling subscriptions"),
        )
        for name, call, what in steps:
            if name in self._teardown_done:
                continue
            deadline = time.monotonic() + bound
            task = asyncio.ensure_future(call())
            while not task.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait({task}, timeout=remaining)
                except asyncio.CancelledError:
                    continue
            if not task.done():
                task.cancel()
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
                self.logger.warning(
                    f"Service '{self.config.name}' {what} did not finish within {bound:g} "
                    f"seconds of its stop being cancelled and was abandoned"
                )
                continue
            try:
                task.result()
                self._teardown_done.add(name)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.logger.exception(f"Error {what} for service '{self.config.name}': {exc}")
                errors.append(exc)

    async def _stop_internal(self, *, from_abortive_startup: bool = False) -> None:
        """Execute internal preliminary teardown and shielded critical resource cleanup.

        Subscriptions are cancelled before the active tasks are drained, and the
        connection is drained and closed last. Intake stops first, so the task
        drain waits only on work already in flight rather than on messages still
        arriving, and the connection's drain then flushes what those tasks sent.
        """
        if self._stopped and not self._running:
            return

        self._running = False
        errors: list[Exception] = []
        cancelled_exc: asyncio.CancelledError | None = None

        if self.hooks is not None:
            # Preliminary teardown
            try:
                # 1. Stop timers
                try:
                    if "stop_timers" not in self._teardown_done:
                        await self.hooks.stop_timers()
                        self._teardown_done.add("stop_timers")
                except Exception as exc:
                    self.logger.exception(
                        f"Error stopping timers for service '{self.config.name}': {exc}"
                    )
                    errors.append(exc)

                # 2. Stop health listener
                try:
                    if "stop_health_listener" not in self._teardown_done:
                        await self.hooks.stop_health_listener()
                        self._teardown_done.add("stop_health_listener")
                except Exception as exc:
                    self.logger.exception(
                        f"Error stopping health listener for service '{self.config.name}': {exc}"
                    )
                    errors.append(exc)

                # 3. Cancel subscriptions
                try:
                    if "cancel_subscriptions" not in self._teardown_done:
                        await self.hooks.cancel_subscriptions()
                        self._teardown_done.add("cancel_subscriptions")
                except Exception as exc:
                    self.logger.exception(
                        f"Error cancelling subscriptions for service '{self.config.name}': {exc}"
                    )
                    errors.append(exc)

                # 4. Drain active tasks
                try:
                    timeout = getattr(self.config, "shutdown_timeout", 30.0)
                    await self.drain_active_tasks(timeout=timeout)
                    self._teardown_done.add("drain_active_tasks")
                except Exception as exc:
                    self.logger.exception(
                        f"Error draining active tasks for service '{self.config.name}': {exc}"
                    )
                    errors.append(exc)

                # 5. User on_shutdown hook
                if self._on_startup_completed:
                    self._on_startup_completed = False
                    try:
                        await self.hooks.on_shutdown()
                    except Exception as exc:
                        self.logger.exception(
                            f"Error in on_shutdown for service '{self.config.name}': {exc}"
                        )
                        errors.append(exc)
            except asyncio.CancelledError as exc:
                cancelled_exc = exc
                await self._release_intake_after_a_cancel(errors)
                if self._on_startup_completed:
                    # Cancelled before `on_shutdown` began, so the cancel jumped past it. It is
                    # still owed: it releases what `on_startup` built, and no later stop() will
                    # run it, because this one marks the service stopped.
                    self._on_startup_completed = False
                    cancelled_exc = await self._on_shutdown_after_a_cancel(errors, exc)

            # Shielded critical teardown
            try:
                # 6. Stop extensions
                try:
                    if "stop_extensions" not in self._teardown_done:
                        await asyncio.shield(self.hooks.stop_extensions())
                        self._teardown_done.add("stop_extensions")
                except Exception as exc:
                    self.logger.exception(
                        f"Error stopping extensions for service '{self.config.name}': {exc}"
                    )
                    errors.append(exc)
                except asyncio.CancelledError as exc:
                    if cancelled_exc is None:
                        cancelled_exc = exc
            finally:
                # 7. Disconnect transport
                try:
                    if "disconnect" not in self._teardown_done:
                        await asyncio.shield(self.hooks.disconnect())
                        self._teardown_done.add("disconnect")
                except Exception as exc:
                    self.logger.exception(
                        f"Error disconnecting from NATS for service '{self.config.name}': {exc}"
                    )
                    errors.append(exc)
                except asyncio.CancelledError as exc:
                    if cancelled_exc is None:
                        cancelled_exc = exc
                finally:
                    if not from_abortive_startup:
                        self._stopped = True
                    elif not errors and cancelled_exc is None:
                        self._stopped = True
        else:
            # No hooks, so nothing to tear down and nothing that can fail: the stop is complete.
            self._stopped = True

        if errors or cancelled_exc is not None:
            self.logger.warning(
                f"Service '{self.config.name}' stop ended with {len(errors)} teardown "
                f"error(s){' and was cancelled' if cancelled_exc is not None else ''}"
            )
        else:
            self.logger.info(f"Service '{self.config.name}' stopped")
        if cancelled_exc is not None:
            raise cancelled_exc
        if errors:
            first, *others = errors
            # The first failure is the one raised, so a caller catching it by type still does.
            # The rest are on it as notes: the disconnect that leaks a socket is often not first.
            for other in others:
                first.add_note(f"teardown also failed: {type(other).__name__}: {other}")
            raise first

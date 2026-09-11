"""The service lifecycle and concurrency state machine.

Governs serialized startup, shutdown, abortive startup cleanup, active task
tracking, and in-flight request draining.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from loguru import logger as global_logger

from .exceptions import ServiceLifecycleError
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


class LifecycleManager:
    """Deterministic state machine governing service startup and shutdown.

    Invariants:
    - Execution of start() and stop() is strictly serialized under ``_lock``.
    - Cancels and awaits in-flight startup task if stop() is called during startup.
    - Cleans up partial resources in abortive startup under ``_lock`` without deadlocks.
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
        self._start_task: asyncio.Task[Any] | None = None
        self._lifecycle_lock: asyncio.Lock | None = None
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def lock(self) -> asyncio.Lock:
        """The reentrant lifecycle mutex."""
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

    @property
    def active_tasks(self) -> set[asyncio.Task[Any]]:
        """Active supervised background tasks currently running."""
        return self._active_tasks

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

    def _on_supervised_task_done(self, task: asyncio.Task[Any]) -> None:
        self._active_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            task_name = task.get_name() if hasattr(task, "get_name") else "unnamed_task"
            self.logger.error(
                f"Unhandled exception in background task '{task_name}': {exc}",
                exc_info=exc,
            )

    async def drain_active_tasks(self, timeout: float | None = 30.0) -> None:
        """Drain active in-flight supervised tasks within the given timeout deadline."""
        if not self._active_tasks:
            return
        if timeout is not None and timeout > 0:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._active_tasks), return_exceptions=True),
                    timeout=timeout,
                )
            except TimeoutError:
                remaining = [t for t in list(self._active_tasks) if not t.done()]
                self.logger.warning(
                    f"Shutdown timeout ({timeout}s) exceeded while draining active tasks "
                    f"for service '{self.config.name}'. Cancelling {len(remaining)} remaining tasks."
                )
                for t in remaining:
                    t.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)
        else:
            await asyncio.gather(*list(self._active_tasks), return_exceptions=True)

    async def start(self) -> None:
        """Execute serialized startup sequence across all subsystems."""
        if self.hooks is None:
            raise RuntimeError("LifecycleManager hooks must be configured before start()")

        if self.stop_requested:
            raise ServiceLifecycleError(
                f"Service '{self.config.name}' cannot start: stop has been requested"
            )

        async with self.lock:
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
            self._start_task = asyncio.current_task()

            try:
                self._loop = asyncio.get_running_loop()

                # 1. Extensions setup
                await self.hooks.setup_extensions()
                if self._is_interrupted():
                    return

                # 2. Handlers discovery
                self.hooks.discover_handlers()

                # 3. Connect to NATS
                await self.hooks.connect()
                if self._is_interrupted():
                    return

                # 4. Stream provisioning & DLQ validation
                if self.hooks.is_jetstream_active():
                    await self.hooks.ensure_streams()
                    self.hooks.validate_dlq()
                if self._is_interrupted():
                    return

                # 5. Service on_startup hook
                await self.hooks.on_startup()
                self._on_startup_completed = True
                if self._is_interrupted():
                    return

                # 6. Start extensions
                await self.hooks.start_extensions()
                if self._is_interrupted():
                    return

                # 7. Start health listener
                await self.hooks.start_health_listener()
                if self._is_interrupted():
                    return

                # 8. Start timers
                await self.hooks.start_timers()
                if self._is_interrupted():
                    return

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
        """Execute serialized teardown of all service resources and subsystems."""
        self._stop_requests += 1
        try:
            if self._starting:
                start_task = self._start_task
                if start_task is not None and start_task is not asyncio.current_task():
                    start_task.cancel()
                    try:
                        await start_task
                    except (asyncio.CancelledError, Exception):
                        pass

            if self._start_task is asyncio.current_task():
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

    async def stop_internal(self, *, from_abortive_startup: bool = False) -> None:
        """Execute internal preliminary teardown and shielded critical resource cleanup."""
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
                    await self.hooks.stop_timers()
                except Exception as exc:
                    self.logger.exception(
                        f"Error stopping timers for service '{self.config.name}': {exc}"
                    )
                    errors.append(exc)

                # 2. Stop health listener
                try:
                    await self.hooks.stop_health_listener()
                except Exception as exc:
                    self.logger.exception(
                        f"Error stopping health listener for service '{self.config.name}': {exc}"
                    )
                    errors.append(exc)

                # 3. Cancel subscriptions
                try:
                    await self.hooks.cancel_subscriptions()
                except Exception as exc:
                    self.logger.exception(
                        f"Error cancelling subscriptions for service '{self.config.name}': {exc}"
                    )
                    errors.append(exc)

                # 4. Drain active tasks
                try:
                    timeout = getattr(self.config, "shutdown_timeout", 30.0)
                    await self.drain_active_tasks(timeout=timeout)
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

            # Shielded critical teardown
            try:
                # 6. Stop extensions
                try:
                    await asyncio.shield(self.hooks.stop_extensions())
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
                    await asyncio.shield(self.hooks.disconnect())
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

        self.logger.info(f"Service '{self.config.name}' stopped")
        if cancelled_exc is not None:
            raise cancelled_exc
        if errors:
            raise errors[0]

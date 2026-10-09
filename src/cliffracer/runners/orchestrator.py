"""Service runner managing process signals and automatic restart backoff."""

import asyncio
import signal
import sys
from typing import Any

from loguru import logger

from ..core import CliffracerService, ServiceConfig
from ..core.construction import apply_config_overlay, construct_service, overlay_refusal_text
from ..core.lifecycle import bounded_shutdown_timeout
from ..core.loop_host import is_abandoned
from ..core.loop_host import run as run_hosted


def _next_backoff(current: float, max_backoff: float) -> float:
    """Return the next exponential-backoff delay, capped at *max_backoff*."""
    return min(current * 2, max_backoff)


def _set_from_signal(event: asyncio.Event, loop: asyncio.AbstractEventLoop | None) -> None:
    """Set *event* from a signal handler, waking the loop that waits on it.

    A Python signal handler runs between bytecodes of the main thread. `Event.set()` only
    schedules the waiters with `call_soon`, which does not wake a loop blocked in `select()`: the
    handler returns, `select` resumes with its old timeout, and the waiters run when it ends. A
    restart backoff is exactly such a wait, up to 60 s long. `call_soon_threadsafe` writes to the
    loop's self-pipe, so the loop wakes at once. Before a loop exists, or once it has closed,
    nothing is waiting and the plain `set()` is the whole job.
    """
    if loop is not None and not loop.is_closed():
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass
        else:
            return
    event.set()


# What a runner's `run()` reports. A runner that ends because it was ASKED to
# stop -- a signal, or `ServiceOrchestrator.stop()` -- succeeded. A runner that
# ends because its service is permanently down did not: nothing is left to
# restart it, the process has no work, and an exit code of 0 tells a
# supervisor that a service which never ran was a success.
RUNNER_OK = 0
RUNNER_SERVICE_DOWN = 1


class _OverlayRefused(ValueError):
    """The config overrides were refused by the service's config; the text names fields, not values."""


class ServiceRunner:
    """Runs services with automatic restart on failure"""

    def __init__(
        self,
        service_class: type[CliffracerService],
        config: ServiceConfig | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.service_class = service_class
        self.config = config
        self.overrides = overrides
        self.service: CliffracerService | None = None
        self._running = False
        # Starts that returned, and attempts made. The two differ exactly when a
        # service is failing to start, which is the case the restart loop exists
        # for, so the log reads attempts and the one-start decision reads starts.
        self._successful_starts = 0
        self._start_attempts = 0
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        # The loop `run()` is running on, so a signal handler can wake it.
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def _log(self) -> Any:
        """The loguru logger bound to the service this runner runs, once its name is known."""
        service = self.service
        name = getattr(getattr(service, "config", None), "name", None) or (
            self.config.name if self.config else None
        )
        return logger.bind(service=name) if name else logger

    def _construct_service(self) -> "CliffracerService":
        """Construct the service and overlay any config overrides."""
        service = construct_service(self.service_class, self.config)
        if self.overrides:
            try:
                apply_config_overlay(service, self.overrides)
            except ValueError as refusal:
                # Not chained: the refusal's frames hold the overrides, credentials among them.
                raise _OverlayRefused(overlay_refusal_text(refusal)) from None
        return service

    def _construct_or_give_up(self) -> "CliffracerService | None":
        """Construct the service, or return None when the failure is permanent.

        A TypeError from construction is an argument mismatch between the
        constructor and the way the runner builds services. Waiting does not
        change a signature, so this is reported once and the runner stops
        rather than retrying it on the restart backoff forever. The same holds
        for an overlay the config refuses: it is the same on every attempt. It is
        reported as the fields and reasons, with no traceback, because a
        traceback prints the overlay and the overlay holds credentials.
        """
        try:
            return self._construct_service()
        except _OverlayRefused as refusal:
            self._log.error(
                f"Cannot apply the config overrides to {self.service_class.__name__}: "
                f"{refusal}. Not retrying."
            )
            return None
        except TypeError:
            self._log.exception(
                f"Cannot construct {self.service_class.__name__}: the constructor does not "
                "accept the arguments the runner supplies. Not retrying."
            )
            return None

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown"""

        def signal_handler(sig: int, frame: Any) -> None:
            self._log.info(f"Received signal {sig}, initiating graceful shutdown...")
            self._running = False
            _set_from_signal(self._shutdown_event, self._loop)

        # Handle Docker stop signals
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        # Windows compatibility
        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, signal_handler)

    def _has_unfinished_shutdown(self) -> bool:
        """Refuse an in-process replacement while the previous instance still works."""
        # Lifecycle-compatible objects can run without owning supervised tasks.
        if not isinstance(self.service, CliffracerService):
            return False
        unfinished = [
            task.get_name()
            for task in self.service.container.lifecycle.active_tasks
            if not task.done() and is_abandoned(task)
        ]
        if unfinished:
            self._log.error(
                "Cannot restart service with unfinished shutdown tasks: "
                + ", ".join(sorted(unfinished))
            )
        return bool(unfinished)

    async def _start(self, service: CliffracerService) -> bool:
        """Start *service*, ending the start at once if a shutdown is requested meanwhile.

        Returns True when the start completed and False when a shutdown cut it off, in which case
        the service has been stopped. `start()` runs on its own task so that `stop()`, which
        cancels the task that is starting the service, cancels the start and not this runner.
        Whatever `start()` raised is raised here.
        """
        start_task = asyncio.create_task(service.start())
        stop_wait = asyncio.create_task(self._shutdown_event.wait())
        try:
            await asyncio.wait({start_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            start_task.cancel()
            await asyncio.gather(start_task, return_exceptions=True)
            raise
        finally:
            stop_wait.cancel()
        if start_task.done():
            start_task.result()
            return True
        self._log.info("Shutdown requested while the service was starting; cancelling the start")
        await service.stop()
        await asyncio.gather(start_task, return_exceptions=True)
        return False

    async def _stop_after_cancel(self) -> None:
        """Stop the service when `run()` is cancelled, for at most the service's grace.

        The stop runs on its own task and is shielded, so a second cancellation does not abandon
        it half done. A stop that outlasts `shutdown_timeout` is reported and cancelled, as the
        loop teardown would cancel it.
        """
        service = self.service
        if service is None:
            return
        stopping = asyncio.ensure_future(service.stop())
        try:
            await asyncio.wait_for(asyncio.shield(stopping), timeout=self._teardown_timeout())
        except TimeoutError:
            self._log.error(
                f"Service '{service.config.name}' did not stop within its shutdown_timeout "
                "after the runner was cancelled; cancelling the stop"
            )
            stopping.cancel()
        except Exception as exc:
            self._log.error(
                f"Stopping service '{service.config.name}' after a cancel failed: {exc}"
            )

    async def _run_service(self) -> None:
        """Run the service with automatic restart"""
        max_backoff = 60
        # Backoff accumulator: seeded lazily from the first constructed instance's
        # restart_delay so the accumulator persists across crash iterations.
        backoff_seconds: float | None = None

        while self._running:
            try:
                # Construct first so restart settings come from the instance's own config.
                service = self._construct_or_give_up()
                if service is None:
                    break
                self.service = service
                name = service.config.name
                if not service.config.auto_restart and self._successful_starts > 0:
                    break

                # Seed the backoff accumulator on first successful construction.
                if backoff_seconds is None:
                    backoff_seconds = self.service.config.restart_delay

                self._start_attempts += 1
                self._log.info(f"Starting service '{name}' (attempt #{self._start_attempts})")
                if not await self._start(self.service):
                    break
                self._successful_starts += 1

                # Reset backoff after a successful start.
                backoff_seconds = self.service.config.restart_delay

                # The event ends the loop as well as `_running`: a set event's `wait()` returns
                # without yielding, so the monitor that clears `_running` would never get to run.
                while self._running and not self._shutdown_event.is_set():
                    # Woken by a stop at once; otherwise once a second, to see a closed broker.
                    try:
                        await asyncio.wait_for(self._shutdown_event.wait(), timeout=1)
                    except TimeoutError:
                        pass
                    from cliffracer.core.container import BrokerConnectionState

                    if getattr(self.service, "broker_state", None) == BrokerConnectionState.CLOSED:
                        self._log.error("NATS connection closed unexpectedly")
                        break

                await self.service.stop()
                if self._has_unfinished_shutdown() or not self.service.config.auto_restart:
                    break

            except asyncio.CancelledError:
                await self._stop_after_cancel()
                raise
            except Exception as e:
                self._log.exception(f"Service crashed: {e}")
                # Seed accumulator if construction itself failed (self.service may be None).
                if backoff_seconds is None:
                    backoff_seconds = self.service.config.restart_delay if self.service else 1.0
                if self.service:
                    try:
                        await self.service.stop()
                    except Exception:
                        pass
                if self._has_unfinished_shutdown():
                    break
                if self._running and (self.service is None or self.service.config.auto_restart):
                    self._log.info(f"Restarting service in {backoff_seconds} seconds...")
                    try:
                        await asyncio.wait_for(self._shutdown_event.wait(), timeout=backoff_seconds)
                        break
                    except TimeoutError:
                        pass
                    # The cap never sits below the configured delay, so a delay above 60 s
                    # stays what it was set to instead of dropping to 60 s on the second wait.
                    backoff_seconds = _next_backoff(
                        backoff_seconds,
                        max(max_backoff, self.service.config.restart_delay)
                        if self.service
                        else max_backoff,
                    )
                else:
                    break

    async def _monitor_shutdown(self) -> None:
        """Monitor for shutdown signal"""
        await self._shutdown_event.wait()
        self._running = False

    async def run(self) -> int:
        """Run the service runner, reporting whether it was asked to stop.

        Returns `RUNNER_OK` when a shutdown was requested and
        `RUNNER_SERVICE_DOWN` when the service task ended on its own: a service
        that is not to be restarted, a permanent construction failure, or a
        broker connection that closed. Those cases leave the process alive with
        nothing to do, and returning 0 for them is what let `cliffracer run`
        exit successfully having never run the service.

        The shutdown event is the discriminator because it is the one fact that
        separates the two: signals set it, `ServiceOrchestrator.stop()` sets it,
        and nothing on the permanently-down paths does.
        """
        # A stop requested before this call (`ServiceOrchestrator.stop()` sets the shared event
        # and clears the flag) is honoured: nothing is started to be stopped at once.
        self._running = not self._shutdown_event.is_set()
        self._loop = asyncio.get_running_loop()

        service_label = self.config.name if self.config else self.service_class.__name__
        self._log.info(f"Starting runner for service '{service_label}'")

        service_task = asyncio.create_task(self._run_service())
        monitor_task = asyncio.create_task(self._monitor_shutdown())
        self._tasks = [service_task, monitor_task]

        # The service task is the one that decides when this runner is done. On a
        # signal the monitor clears _running, the service task drains and returns,
        # and awaiting it here keeps that drain. When the service task instead
        # finishes on its own -- a service that is not to be restarted, or a
        # permanent construction failure -- nothing will ever set the shutdown
        # event, so the monitor is cancelled rather than waited on.
        try:
            await service_task
        except Exception:
            self._log.exception(f"Runner for service '{service_label}' failed")
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)

        if self._shutdown_event.is_set():
            self._log.info(f"Runner for service '{service_label}' stopped")
            return RUNNER_OK

        self._log.error(
            f"Runner for service '{service_label}' stopped because the service is "
            "permanently down: nothing is left to restart it. Exiting non-zero so a "
            "supervisor restarts the process rather than leaving it alive and idle."
        )
        return RUNNER_SERVICE_DOWN

    def _teardown_timeout(self) -> float | None:
        """Read the constructed service's cancellation grace, including overrides."""
        config = self.service.config if self.service is not None else self.config
        if config is None:
            return 30.0
        return bounded_shutdown_timeout(
            config.shutdown_timeout, self._log, "The runner's bound on a stop after a cancel"
        )

    def run_forever(self) -> None:
        """Synchronous entry point. Exits non-zero if the service went down.

        Installs the process's SIGTERM and SIGINT handlers: that is a property of the process,
        so it belongs to the entry point that owns the process. `run()` leaves them alone, which
        is what lets a runner be awaited inside a larger program without taking its signals.
        """
        self._setup_signal_handlers()
        try:
            status = run_hosted(self.run(), teardown_timeout=self._teardown_timeout)
        except KeyboardInterrupt:
            self._log.info("Keyboard interrupt received")
            return
        except Exception as e:
            self._log.exception(f"Fatal error in runner: {e}")
            sys.exit(1)
        if status != RUNNER_OK:
            sys.exit(status)


class ServiceOrchestrator:
    """Run multiple services in parallel"""

    def __init__(self, log_level: str | None = None) -> None:
        self.runners: list[ServiceRunner] = []
        self._shutdown_event = asyncio.Event()
        self._log_level = log_level
        #: Set when `run()` has returned; None until it has been entered.
        self._finished: asyncio.Event | None = None
        # The loop `run()` is running on, so a signal handler can wake it.
        self._loop: asyncio.AbstractEventLoop | None = None

    def _configure_logging(self) -> None:
        """Point loguru at one sink for the whole process.

        Sinks are process-global, so this is a process-wide setting and says
        so: every service running here logs at the same level. It runs before
        any service starts, so nothing is emitted through the default sink
        first.
        """
        if self._log_level is None:
            return
        logger.remove()
        # A traceback shows its frames and not the values in them (`diagnose=False`): the
        # values include the credentials a service was configured with.
        logger.add(sys.stderr, level=self._log_level, diagnose=False)

    def add_service(
        self,
        service_class: type[CliffracerService],
        config: ServiceConfig | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        """Add a service to run.

        ``config`` is optional — services self-configure by default. Pass
        ``overrides`` (a dict of ``ServiceConfig`` field values) to overlay
        settings such as ``nats_url`` onto the service's own config at
        construction time.
        """
        runner = ServiceRunner(service_class, config=config, overrides=overrides)
        self.runners.append(runner)

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown"""

        def signal_handler(sig: int, frame: Any) -> None:
            logger.info(f"Received signal {sig}, shutting down all services...")
            _set_from_signal(self._shutdown_event, self._loop)

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, signal_handler)

    async def _run_all_services(self) -> int:
        """Run all services concurrently, reporting the worst outcome.

        One permanently-down service is enough to make the process a failure:
        the orchestrator goes on running the others, and a supervisor reading
        the exit code is the only thing that will notice the gap.

        A runner that raised is counted as down. `gather(return_exceptions=True)`
        turns that into a value rather than a raise, and treating it as a
        success would report 0 for a runner that never returned a status.
        """
        tasks = []

        for runner in self.runners:
            # Share shutdown event
            runner._shutdown_event = self._shutdown_event
            tasks.append(asyncio.create_task(runner.run()))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        statuses = [r if isinstance(r, int) else RUNNER_SERVICE_DOWN for r in results]
        return max(statuses, default=RUNNER_OK)

    async def run(self) -> int:
        """Run all services, reporting the worst runner outcome."""
        self._configure_logging()
        self._finished = asyncio.Event()
        self._loop = asyncio.get_running_loop()

        logger.info(f"Starting {len(self.runners)} services")

        try:
            status = await self._run_all_services()
        finally:
            self._finished.set()

        logger.info("All services stopped")
        return status

    async def stop(self) -> None:
        """Gracefully stop all services, returning once `run()` has finished.

        Triggers the shared shutdown event (the same mechanism the signal
        handlers use), so each runner exits its run loop and tears its service
        down, then waits for `run()` to return. Called before `run()` has been
        entered it only records the request. A service that awaits this from one
        of its own handlers waits for its own teardown, which cancels that
        handler after `shutdown_timeout`; stop from outside the services.
        """
        logger.info("Stopping all services")
        for runner in self.runners:
            runner._running = False
        self._shutdown_event.set()
        if self._finished is not None:
            await self._finished.wait()

    def _teardown_timeout(self) -> float | None:
        """Use the longest grace, respecting services that explicitly wait forever."""
        timeouts = [runner._teardown_timeout() for runner in self.runners]
        if any(timeout is None for timeout in timeouts):
            return None
        return max((timeout for timeout in timeouts if timeout is not None), default=30.0)

    def run_forever(self) -> None:
        """Synchronous entry point. Exits non-zero if any service went down.

        Installs the process's SIGTERM and SIGINT handlers, which end every service through the
        shared shutdown event. The runners it drives install none of their own.
        """
        self._setup_signal_handlers()
        try:
            status = run_hosted(self.run(), teardown_timeout=self._teardown_timeout)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
            return
        except Exception as e:
            logger.exception(f"Fatal error in multi-runner: {e}")
            sys.exit(1)
        if status != RUNNER_OK:
            sys.exit(status)

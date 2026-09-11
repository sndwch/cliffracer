"""Service runner managing process signals and automatic restart backoff."""

import asyncio
import inspect
import signal
import sys
from collections.abc import Callable
from typing import Any, cast

from loguru import logger

from ..core import CliffracerService, ServiceConfig


def _next_backoff(current: float, max_backoff: float) -> float:
    """Return the next exponential-backoff delay, capped at *max_backoff*."""
    return min(current * 2, max_backoff)


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
        self._restart_count = 0
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []

    def _constructor_takes_config(self) -> bool:
        """True if the service class constructor accepts a positional config arg."""
        try:
            sig = inspect.signature(self.service_class)
        except (TypeError, ValueError):
            return False
        return len(sig.parameters) >= 1

    def _apply_overlay(self, service: "CliffracerService", values: dict[str, Any]) -> None:
        """Overlay config field values onto the constructed service's config."""
        for key, value in values.items():
            if not hasattr(service.config, key):
                raise ValueError(f"Unknown ServiceConfig field in overrides: {key!r}")
            setattr(service.config, key, value)

    def _construct_service(self) -> "CliffracerService":
        """Construct the service and overlay any config overrides.

        Self-configuring services (no-arg ``__init__``) are the convention; the
        instance builds its own ``config``. If the constructor accepts a config
        arg, the supplied ``config`` is passed through. A legacy ``ServiceConfig``
        passed to a no-arg class is overlaid (minus ``name``) for back-compat.
        """
        if self.config is not None and self._constructor_takes_config():
            service = self.service_class(self.config)
        else:
            service = cast(Callable[[], CliffracerService], self.service_class)()
            if self.config is not None:
                # exclude_unset (not exclude_defaults): an explicitly-passed legacy
                # value wins even when it equals the schema default.
                legacy = self.config.model_dump(exclude_unset=True)
                legacy.pop("name", None)
                self._apply_overlay(service, legacy)
        if self.overrides:
            self._apply_overlay(service, self.overrides)
        return service

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown"""

        def signal_handler(sig: int, frame: Any) -> None:
            logger.info(f"Received signal {sig}, initiating graceful shutdown...")
            self._running = False
            self._shutdown_event.set()

        # Handle Docker stop signals
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        # Windows compatibility
        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, signal_handler)

    async def _run_service(self) -> None:
        """Run the service with automatic restart"""
        max_backoff = 60
        # Backoff accumulator: seeded lazily from the first constructed instance's
        # restart_delay so the accumulator persists across crash iterations.
        backoff_seconds: float | None = None

        while self._running:
            try:
                # Construct first so restart settings come from the instance's own config.
                self.service = self._construct_service()
                name = self.service.config.name
                if not self.service.config.auto_restart and self._restart_count > 0:
                    break

                # Seed the backoff accumulator on first successful construction.
                if backoff_seconds is None:
                    backoff_seconds = self.service.config.restart_delay

                logger.info(f"Starting service '{name}' (attempt #{self._restart_count + 1})")
                await self.service.start()
                self._restart_count += 1

                # Reset backoff after a successful start.
                backoff_seconds = self.service.config.restart_delay

                while self._running:
                    await asyncio.sleep(1)
                    from cliffracer.core.container import BrokerConnectionState

                    if getattr(self.service, "broker_state", None) == BrokerConnectionState.CLOSED:
                        logger.error("NATS connection closed unexpectedly")
                        break

                await self.service.stop()
                if not self.service.config.auto_restart:
                    break

            except Exception as e:
                logger.exception(f"Service crashed: {e}")
                # Seed accumulator if construction itself failed (self.service may be None).
                if backoff_seconds is None:
                    backoff_seconds = self.service.config.restart_delay if self.service else 1.0
                if self.service:
                    try:
                        await self.service.stop()
                    except Exception:
                        pass
                if self._running and (self.service is None or self.service.config.auto_restart):
                    logger.info(f"Restarting service in {backoff_seconds} seconds...")
                    try:
                        await asyncio.wait_for(self._shutdown_event.wait(), timeout=backoff_seconds)
                        break
                    except TimeoutError:
                        pass
                    backoff_seconds = _next_backoff(backoff_seconds, max_backoff)
                else:
                    break

    async def _monitor_shutdown(self) -> None:
        """Monitor for shutdown signal"""
        await self._shutdown_event.wait()
        self._running = False

    async def run(self) -> None:
        """Run the service runner"""
        self._running = True
        self._setup_signal_handlers()

        service_label = self.config.name if self.config else self.service_class.__name__
        logger.info(f"Starting runner for service '{service_label}'")

        # Create tasks
        self._tasks = [
            asyncio.create_task(self._run_service()),
            asyncio.create_task(self._monitor_shutdown()),
        ]

        # Wait for shutdown
        await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info(f"Runner for service '{service_label}' stopped")

    def run_forever(self) -> None:
        """Synchronous entry point"""
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as e:
            logger.exception(f"Fatal error in runner: {e}")
            sys.exit(1)


class ServiceOrchestrator:
    """Run multiple services in parallel"""

    def __init__(self) -> None:
        self.runners: list[ServiceRunner] = []
        self._running = False
        self._shutdown_event = asyncio.Event()

    def add_service(
        self,
        service_class: type[CliffracerService],
        config: ServiceConfig | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        """Add a service to run.

        ``config`` is optional — services self-configure by default. Pass
        ``overrides`` (a dict of ``ServiceConfig`` field values) to overlay
        settings such as ``nats_url`` or ``log_level`` onto the service's
        own config at construction time.
        """
        runner = ServiceRunner(service_class, config=config, overrides=overrides)
        self.runners.append(runner)

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown"""

        def signal_handler(sig: int, frame: Any) -> None:
            logger.info(f"Received signal {sig}, shutting down all services...")
            self._running = False
            self._shutdown_event.set()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, signal_handler)

    async def _run_all_services(self) -> None:
        """Run all services concurrently"""
        tasks = []

        for runner in self.runners:
            # Share shutdown event
            runner._shutdown_event = self._shutdown_event
            tasks.append(asyncio.create_task(runner.run()))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self) -> None:
        """Run all services"""
        self._running = True
        self._setup_signal_handlers()

        logger.info(f"Starting {len(self.runners)} services")

        await self._run_all_services()

        logger.info("All services stopped")

    async def stop(self) -> None:
        """Gracefully stop all services.

        Triggers the shared shutdown event (the same mechanism the signal
        handlers and ServiceRunner use), so each runner exits its run loop
        and tears its service down cleanly.
        """
        logger.info("Stopping all services")
        self._running = False
        for runner in self.runners:
            runner._running = False
        self._shutdown_event.set()

    def run_forever(self) -> None:
        """Synchronous entry point"""
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as e:
            logger.exception(f"Fatal error in multi-runner: {e}")
            sys.exit(1)

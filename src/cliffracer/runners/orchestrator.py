"""
Service runner with automatic restart capability
Handles graceful shutdown and Docker signals
"""

import asyncio
import logging
import signal
import sys

from ..core import NATSService, ServiceConfig

logger = logging.getLogger(__name__)


class ServiceRunner:
    """Runs services with automatic restart on failure"""

    def __init__(self, service_class: type[NATSService], config: ServiceConfig):
        self.service_class = service_class
        self.config = config
        self.service: NATSService | None = None
        self._running = False
        self._restart_count = 0
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown"""

        def signal_handler(sig, frame):
            logger.info(f"Received signal {sig}, initiating graceful shutdown...")
            self._running = False
            self._shutdown_event.set()

        # Handle Docker stop signals
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        # Windows compatibility
        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, signal_handler)

    async def _run_service(self):
        """Run the service with automatic restart"""
        backoff_seconds = 1
        max_backoff = 60

        while self._running and self.config.auto_restart:
            try:
                logger.info(
                    f"Starting service '{self.config.name}' (attempt #{self._restart_count + 1})"
                )

                # Create and start service
                self.service = self.service_class(self.config)
                await self.service.start()

                # Reset backoff on successful start
                backoff_seconds = 1
                self._restart_count += 1

                # Keep service running
                while self._running:
                    await asyncio.sleep(1)

                    # Check if service is still healthy
                    if self.service.nc and self.service.nc.is_closed:
                        logger.error("NATS connection closed unexpectedly")
                        break

                # Graceful shutdown
                await self.service.stop()

            except Exception as e:
                logger.error(f"Service '{self.config.name}' crashed: {e}", exc_info=True)

                if self.service:
                    try:
                        await self.service.stop()
                    except Exception:
                        pass

                if self._running and self.config.auto_restart:
                    logger.info(f"Restarting service in {backoff_seconds} seconds...")
                    await asyncio.sleep(backoff_seconds)

                    # Exponential backoff
                    backoff_seconds = min(backoff_seconds * 2, max_backoff)
                else:
                    break

    async def _monitor_shutdown(self):
        """Monitor for shutdown signal"""
        await self._shutdown_event.wait()
        self._running = False

    async def run(self):
        """Run the service runner"""
        self._running = True
        self._setup_signal_handlers()

        logger.info(f"Starting runner for service '{self.config.name}'")

        # Create tasks
        self._tasks = [
            asyncio.create_task(self._run_service()),
            asyncio.create_task(self._monitor_shutdown()),
        ]

        # Wait for shutdown
        await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info(f"Runner for service '{self.config.name}' stopped")

    def run_forever(self):
        """Synchronous entry point"""
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as e:
            logger.error(f"Fatal error in runner: {e}", exc_info=True)
            sys.exit(1)


class ServiceOrchestrator:
    """Run multiple services in parallel"""

    def __init__(self):
        self.runners: list[ServiceRunner] = []
        self._running = False
        self._shutdown_event = asyncio.Event()

    def add_service(self, service_class: type[NATSService], config: ServiceConfig):
        """Add a service to run"""
        runner = ServiceRunner(service_class, config)
        self.runners.append(runner)

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown"""

        def signal_handler(sig, frame):
            logger.info(f"Received signal {sig}, shutting down all services...")
            self._running = False
            self._shutdown_event.set()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, signal_handler)

    async def _run_all_services(self):
        """Run all services concurrently"""
        tasks = []

        for runner in self.runners:
            # Share shutdown event
            runner._shutdown_event = self._shutdown_event
            tasks.append(asyncio.create_task(runner.run()))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        """Run all services"""
        self._running = True
        self._setup_signal_handlers()

        logger.info(f"Starting {len(self.runners)} services")

        await self._run_all_services()

        logger.info("All services stopped")

    def run_forever(self):
        """Synchronous entry point"""
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as e:
            logger.error(f"Fatal error in multi-runner: {e}", exc_info=True)
            sys.exit(1)


def configure_logging(level=logging.INFO):
    """Configure logging for services"""
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Reduce noise from NATS client
    logging.getLogger("nats").setLevel(logging.WARNING)

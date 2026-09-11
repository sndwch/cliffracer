"""
Example: LoggingExtension for service logging
=============================================

Demonstrates attaching LoggingExtension to a CliffracerService to provide
dispatch timing, structured logging, and optional log streaming to NATS.

Requires the `cliffracer-logging` distribution; `uv sync --all-packages`
installs it from this workspace.
"""

import asyncio

from cliffracer_logging import LoggingConfig, LoggingExtension
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc


class Processed(BaseModel):
    processed: str
    length: int


# Example 1: timing logs for every dispatch, which is the default
class LoggedService(CliffracerService):
    """Service with default logging extension enabled."""

    logging = LoggingExtension()

    @rpc
    async def process_data(self, data: str) -> Processed:
        # Called over NATS, worker_result logs "rpc <subject> <ms>ms" at DEBUG.
        # Called directly, as main() does below, it does NOT: the hook chain is
        # the container's, and a direct call never reaches the container.
        return Processed(processed=data.upper(), length=len(data))


# Example 2: stream this service's logs to NATS as well
class StreamingService(CliffracerService):
    """Service configured to stream log records to NATS."""

    logging = LoggingExtension(to_nats=True)

    @rpc
    async def echo(self, value: str) -> str:
        return value


# Example 3: timing off, sink on
class QuietStreamingService(CliffracerService):
    logging = LoggingExtension(to_nats=True, timing=False)


async def main():
    print("[INFO] LoggingExtension example")
    print("=" * 50)

    # Configure structured JSON logging.
    LoggingConfig.configure(service_name="logging_example", log_level="DEBUG")

    service = LoggedService(ServiceConfig(name="example_logged"))
    await service.container.setup_extensions()

    service.logger.info("a manually logged message")

    # Direct call: no timing line, for the reason on the method above. Start
    # the service against a broker and call it over NATS to see those.
    result = await service.process_data("hello world")
    print(f"Result: {result}")

    print("\n[OK] What the extension gives you:")
    print("1. Declarative extension configuration on the service class")
    print("2. Timing on EVERY dispatch, because the container drives the chain")
    print("3. A NATS sink whose lifetime is tied to the service's")
    print("4. health_details() reporting whether it is actually streaming")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())

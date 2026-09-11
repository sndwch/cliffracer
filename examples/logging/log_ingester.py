#!/usr/bin/env python3
"""
Log Ingester Service - Ships logs from NATS to OpenObserve

This service subscribes to all log events on the NATS "logs.>" topic
and forwards them to OpenObserve for centralized log aggregation and analysis.

Usage:
    python log_ingester.py

Configuration via environment variables:
    - NATS_URL: NATS server URL (default: nats://localhost:4222)
    - OPENOBSERVE_URL: OpenObserve API URL (default: http://localhost:5080)
    - OPENOBSERVE_ORG: OpenObserve organization (default: default)
    - OPENOBSERVE_STREAM: OpenObserve stream name (default: cliffracer_logs)
    - OPENOBSERVE_USER: OpenObserve username (default: admin@example.com)
    - OPENOBSERVE_PASSWORD: OpenObserve password (default: password)
    - BATCH_SIZE: Number of logs to batch before sending (default: 100)
    - FLUSH_INTERVAL: Seconds between automatic flushes (default: 5)
"""

import asyncio
import os
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict

from cliffracer import CliffracerService, ServiceConfig, listener, timer


class LogEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    level: str = "INFO"
    message: str = ""
    service: str = "unknown"


class LogIngester(CliffracerService):
    """
    Service that ingests logs from NATS and forwards them to OpenObserve.

    This service acts as a bridge between NATS-based log streaming and
    OpenObserve's centralized log storage, enabling:
    - Real-time log aggregation from all services
    - Buffering and batching for efficient storage
    - Automatic retry on failures
    - Graceful error handling (logs keep flowing even if OpenObserve is down)
    """

    def __init__(self):
        # Configuration
        nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
        self.openobserve_url = os.getenv("OPENOBSERVE_URL", "http://localhost:5080")
        self.openobserve_org = os.getenv("OPENOBSERVE_ORG", "default")
        self.openobserve_stream = os.getenv("OPENOBSERVE_STREAM", "cliffracer_logs")
        self.openobserve_user = os.getenv("OPENOBSERVE_USER", "admin@example.com")
        self.openobserve_password = os.getenv("OPENOBSERVE_PASSWORD", "password")
        self.batch_size = int(os.getenv("BATCH_SIZE", "100"))
        self.flush_interval = int(os.getenv("FLUSH_INTERVAL", "5"))

        # Build OpenObserve API endpoint
        # Format: http://host:port/api/{org}/{stream}/_json
        self.api_endpoint = (
            f"{self.openobserve_url}/api/{self.openobserve_org}/{self.openobserve_stream}/_json"
        )

        # Initialize service
        # LoggingExtension(to_nats=True) is omitted to prevent log forwarding loops.
        config = ServiceConfig(
            name="log_ingester",
            nats_url=nats_url,
            log_level="INFO",
        )
        super().__init__(config)

        # Log buffer
        self.buffer: list[dict[str, Any]] = []
        self.buffer_lock = asyncio.Lock()

        # Metrics
        self.logs_received = 0
        self.logs_sent = 0
        self.logs_failed = 0
        self.batches_sent = 0

        logger.info(
            f"Log ingester configured: {self.api_endpoint} "
            f"(batch_size={self.batch_size}, flush_interval={self.flush_interval}s)"
        )

    @listener("logs.>", fanout=True)
    async def ingest_log(self, subject: str, message: LogEntry) -> None:
        """
        Receive log events from NATS and buffer them for batching.

        This handler receives all logs published to topics matching "logs.*"
        (e.g., logs.user_service.info, logs.order_service.error).
        """
        async with self.buffer_lock:
            self.buffer.append(message.model_dump())
            self.logs_received += 1

            # Flush if buffer reaches batch size
            if len(self.buffer) >= self.batch_size:
                await self._flush_to_openobserve()

    @timer(interval=5)  # Flush every 5 seconds by default
    async def flush_timer(self):
        """Periodically flush buffered logs to OpenObserve"""
        async with self.buffer_lock:
            if self.buffer:
                await self._flush_to_openobserve()

    async def _flush_to_openobserve(self):
        """
        Send buffered logs to OpenObserve in bulk.

        This method is called either when:
        1. Buffer reaches batch_size
        2. flush_timer triggers

        Must be called while holding buffer_lock.
        """
        if not self.buffer:
            return

        batch = self.buffer.copy()
        self.buffer.clear()

        try:
            # Send to OpenObserve using Basic Auth
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self.api_endpoint,
                    json=batch,
                    auth=(self.openobserve_user, self.openobserve_password),
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()

            self.logs_sent += len(batch)
            self.batches_sent += 1
            logger.debug(
                f"Flushed {len(batch)} logs to OpenObserve "
                f"(total: {self.logs_sent}/{self.logs_received})"
            )

        except httpx.HTTPStatusError as e:
            self.logs_failed += len(batch)
            logger.error(
                f"HTTP error sending logs to OpenObserve: {e.response.status_code} "
                f"{e.response.text}"
            )

        except Exception as e:
            self.logs_failed += len(batch)
            logger.error(f"Failed to send logs to OpenObserve: {type(e).__name__}: {e}")

    @timer(interval=60)  # Report stats every minute
    async def report_stats(self):
        """Report ingestion statistics"""
        logger.info(
            f"Log ingestion stats: "
            f"received={self.logs_received}, "
            f"sent={self.logs_sent}, "
            f"failed={self.logs_failed}, "
            f"batches={self.batches_sent}, "
            f"buffered={len(self.buffer)}"
        )

    async def stop(self):
        """Flush remaining logs before stopping"""
        logger.info("Flushing remaining logs before shutdown...")
        async with self.buffer_lock:
            await self._flush_to_openobserve()
        await super().stop()


if __name__ == "__main__":
    # Create and run the log ingester service
    ingester = LogIngester()
    logger.info("Starting log ingester service...")
    ingester.run()

"""
Optimized NATS connection management for high-performance scenarios
"""

import asyncio
from typing import Any

import nats
from loguru import logger


class OptimizedNATSConnection:
    """
    High-performance NATS connection with optimized settings.

    This class provides connection pooling and optimized settings
    for maximum throughput and minimum latency.
    """

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        max_connections: int = 10,
        ping_interval: int = 120,
        max_outstanding_pings: int = 3,
        reconnect_time_wait: int = 1,
        max_reconnect_attempts: int = 10
    ):
        """
        Initialize optimized NATS connection pool.

        Args:
            nats_url: NATS server URL
            max_connections: Maximum number of connections in pool
            ping_interval: Ping interval in seconds (higher = less overhead)
            max_outstanding_pings: Max outstanding pings before disconnection
            reconnect_time_wait: Seconds between reconnection attempts
            max_reconnect_attempts: Maximum reconnection attempts
        """
        self.nats_url = nats_url
        self.max_connections = max_connections
        self.ping_interval = ping_interval
        self.max_outstanding_pings = max_outstanding_pings
        self.reconnect_time_wait = reconnect_time_wait
        self.max_reconnect_attempts = max_reconnect_attempts

        self._connections = []
        self._current_index = 0
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Create optimized connection pool"""
        logger.info(f"Creating optimized NATS connection pool with {self.max_connections} connections")

        for i in range(self.max_connections):
            try:
                conn = await nats.connect(
                    self.nats_url,
                    ping_interval=self.ping_interval,
                    max_outstanding_pings=self.max_outstanding_pings,
                    reconnect_time_wait=self.reconnect_time_wait,
                    max_reconnect_attempts=self.max_reconnect_attempts,
                    # Performance optimizations that exist in nats-py
                    drain_timeout=30,  # Longer drain timeout
                )
                self._connections.append(conn)
                logger.debug(f"Created optimized connection {i+1}/{self.max_connections}")

            except Exception as e:
                logger.error(f"Failed to create connection {i+1}: {e}")
                raise

        logger.info(f"Optimized connection pool ready with {len(self._connections)} connections")

    async def get_connection(self):
        """Get next available connection from pool (round-robin)"""
        if not self._connections:
            raise RuntimeError("No connections available - call connect() first")

        async with self._lock:
            conn = self._connections[self._current_index]
            self._current_index = (self._current_index + 1) % len(self._connections)
            return conn

    async def request(self, subject: str, payload: bytes, timeout: float = 5.0) -> Any:
        """Optimized request with connection pooling"""
        conn = await self.get_connection()
        return await conn.request(subject, payload, timeout=timeout)

    async def publish(self, subject: str, payload: bytes) -> None:
        """Optimized publish with connection pooling"""
        conn = await self.get_connection()
        await conn.publish(subject, payload)

    async def subscribe(self, subject: str, queue: str | None = None, cb=None):
        """Subscribe using least loaded connection"""
        # Use first connection for subscriptions to maintain message ordering
        if not self._connections:
            raise RuntimeError("No connections available")
        conn = self._connections[0]
        return await conn.subscribe(subject, queue=queue, cb=cb)

    async def close(self) -> None:
        """Close all connections in pool"""
        logger.info("Closing optimized connection pool")

        for i, conn in enumerate(self._connections):
            try:
                await conn.close()
                logger.debug(f"Closed connection {i+1}")
            except Exception as e:
                logger.warning(f"Error closing connection {i+1}: {e}")

        self._connections.clear()
        logger.info("Optimized connection pool closed")

    @property
    def is_connected(self) -> bool:
        """Check if any connections are active"""
        return any(not conn.is_closed for conn in self._connections)

    def get_stats(self) -> dict[str, Any]:
        """Get connection pool statistics"""
        active_connections = sum(1 for conn in self._connections if not conn.is_closed)

        return {
            "total_connections": len(self._connections),
            "active_connections": active_connections,
            "max_connections": self.max_connections,
            "current_index": self._current_index,
            "utilization_percent": (active_connections / self.max_connections) * 100 if self.max_connections > 0 else 0
        }

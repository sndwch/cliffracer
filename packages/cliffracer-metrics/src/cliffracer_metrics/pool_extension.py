"""NATS connection pool lifecycle extension."""

from typing import Any

from loguru import logger

from cliffracer.core.extension import Extension, ExtensionSetupContext

from .connection_pool import OptimizedNATSConnection


class PoolExtension(Extension):
    """Declare PoolExtension to attach a connected OptimizedNATSConnection pool.

    class Ingest(CliffracerService):
        pool = PoolExtension(max_connections=8)

        @rpc
        async def bulk(self, subject: str) -> dict[str, bool]:
            reply = await self.pool.request(subject, b"{}")
            return {"ok": True}
    """

    def __init__(
        self,
        max_connections: int = 10,
        ping_interval: int = 120,
        max_outstanding_pings: int = 3,
        reconnect_time_wait: int = 1,
        max_reconnect_attempts: int = 10,
    ) -> None:
        self._settings = {
            "max_connections": max_connections,
            "ping_interval": ping_interval,
            "max_outstanding_pings": max_outstanding_pings,
            "reconnect_time_wait": reconnect_time_wait,
            "max_reconnect_attempts": max_reconnect_attempts,
        }
        # Instance state initialized in setup() for per-service isolation.
        self.pool: OptimizedNATSConnection | None = None

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        # Credentials extracted from service configuration.
        self.pool = OptimizedNATSConnection(
            nats_url=ctx.broker_url,
            auth_kwargs=ctx.service_config.nats_auth_kwargs(),
            **self._settings,
            service=ctx.service,
        )

    async def start(self) -> None:
        if self.pool is None:
            raise RuntimeError("PoolExtension.setup() must be called before start()")
        await self.pool.connect()
        logger.info(f"{self.name}: pool of {self._settings['max_connections']} connections ready")

    async def stop(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def health_details(self) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        return {
            "connections": len(self.pool._connections),
            "connected": self.pool.is_connected,
        }

    # Forward common client methods to the underlying pool instance.
    async def request(self, subject: str, payload: bytes, timeout: float = 5.0) -> Any:
        if self.pool is None:
            raise RuntimeError("Pool not initialized")
        return await self.pool.request(subject, payload, timeout=timeout)

    async def publish(self, subject: str, payload: bytes) -> None:
        if self.pool is None:
            raise RuntimeError("Pool not initialized")
        await self.pool.publish(subject, payload)

    async def get_connection(self) -> Any:
        if self.pool is None:
            raise RuntimeError("Pool not initialized")
        return await self.pool.get_connection()

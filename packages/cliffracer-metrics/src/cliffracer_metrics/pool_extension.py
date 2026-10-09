"""NATS connection pool lifecycle extension."""

import uuid
from typing import Any

from loguru import logger

from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.extension import Extension, ExtensionSetupContext
from cliffracer.core.lifecycle import bounded_shutdown_timeout

from .connection_pool import OptimizedNATSConnection


class PoolExtension(Extension):
    """Declare PoolExtension to attach a connected OptimizedNATSConnection pool.

    The pool connects as its service does: `ping_interval`, `max_outstanding_pings`,
    `reconnect_time_wait` and `max_reconnect_attempts` default to the service's own settings
    (a service that sets no ping settings leaves nats-py's defaults in force), and a value
    given here replaces them; its credentials, inbox prefix and `connect_timeout` are the
    service's, and each connection is named `<service>-pool-<n>` on the broker.

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
        ping_interval: float | None = None,
        max_outstanding_pings: int | None = None,
        reconnect_time_wait: int | None = None,
        max_reconnect_attempts: int | None = None,
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
        # The ping and reconnect settings are the service's own unless one is given here: a pooled
        # client that gives up while the service keeps reconnecting is closed for good.
        settings: dict[str, Any] = {
            key: value if value is not None else getattr(ctx.service_config, key)
            for key, value in self._settings.items()
        }
        # Credentials and inbox prefix from the service's configuration, its name and its
        # connect timeout: a pooled connection is the service's own, more than once.
        self.pool = OptimizedNATSConnection(
            nats_url=ctx.broker_url,
            auth_kwargs=ctx.service_config.nats_connect_kwargs(),
            name=ctx.service_config.name,
            connect_timeout=ctx.service_config.connect_timeout,
            drain_timeout=bounded_shutdown_timeout(
                ctx.service_config.shutdown_timeout,
                logger.bind(service=ctx.service_config.name),
                "Draining the connection pool",
            ),
            **settings,
            service=ctx.service,
        )

    async def start(self) -> None:
        if self.pool is None:
            raise RuntimeError("PoolExtension.setup() must be called before start()")
        await self.pool.connect()
        self._service_log.info(
            f"{self.name}: pool of {self._settings['max_connections']} connections ready"
        )

    async def stop(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def health_details(self) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        stats = self.pool.get_stats()
        return {
            "connections": stats["total_connections"],
            "active_connections": stats["active_connections"],
            "closed_connections": stats["closed_connections"],
            "connected": self.pool.is_connected,
            "service_connected": stats["service_connected"],
        }

    # Forward common client methods to the underlying pool instance.
    @staticmethod
    def _with_correlation(headers: dict[str, str] | None) -> dict[str, str]:
        """The caller's headers plus the correlation id this call belongs to.

        The order `ServiceClient` uses: an id the caller set in `headers`, then the ambient one
        of the request being handled, then a new one, sent as `X-Correlation-ID` and
        `correlation_id`, so the service that answers is a hop of the same trace.
        """
        given = dict(headers or {})
        cid = (
            CorrelationContext.extract_from_headers(given)
            or CorrelationContext.ambient_for_send()
            or uuid.uuid4().hex
        )
        kept = {
            name: value
            for name, value in given.items()
            if name.lower() not in {"x-correlation-id", "correlation_id"}
        }
        return {**kept, "X-Correlation-ID": cid, "correlation_id": cid}

    async def request(
        self,
        subject: str,
        payload: bytes,
        timeout: float = 5.0,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if self.pool is None:
            raise RuntimeError("Pool not initialized")
        return await self.pool.request(
            subject, payload, timeout=timeout, headers=self._with_correlation(headers)
        )

    async def publish(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Pool not initialized")
        await self.pool.publish(subject, payload, headers=self._with_correlation(headers))

    async def get_connection(self) -> Any:
        if self.pool is None:
            raise RuntimeError("Pool not initialized")
        return await self.pool.get_connection()

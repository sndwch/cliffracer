"""NATS connection pool management."""

import asyncio
import builtins
from typing import Any

from loguru import logger
from nats.errors import Error as NatsError

from cliffracer.core import dial


class _NoDrain(Exception):
    """Raised inside `_shutdown` to go straight to closing a connection."""


class OptimizedNATSConnection:
    """Connection pool maintaining multiple concurrent NATS client connections."""

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        max_connections: int = 10,
        ping_interval: float | None = 120,
        max_outstanding_pings: int | None = 3,
        reconnect_time_wait: int = 1,
        max_reconnect_attempts: int = 10,
        auth_kwargs: dict | None = None,
        service: Any = None,
        name: str | None = None,
        connect_timeout: float | None = None,
        drain_timeout: float | None = 30.0,
    ):
        """
        Initialize optimized NATS connection pool.

        Args:
            nats_url: NATS server URL
            max_connections: Maximum number of connections in pool
            ping_interval: Ping interval in seconds (higher = less overhead), or None for
                nats-py's own default
            max_outstanding_pings: Max outstanding pings before disconnection, or None for
                nats-py's own default
            reconnect_time_wait: Seconds between reconnection attempts
            max_reconnect_attempts: Maximum reconnection attempts
            auth_kwargs: Keyword arguments for ``nats.connect``: credentials and inbox prefix
            name: The service's name; each connection is named ``<name>-pool-<n>`` on the broker
            connect_timeout: Seconds each connection may take to be made, or None for no bound
            drain_timeout: Seconds ``close()`` may spend in all, waiting for the requests in flight
                and then draining the connections (which drain at the same time), or None for
                no bound
        """
        self.nats_url = nats_url
        # Explicit credentials ensure consistency with service connection configuration.
        self.auth_kwargs = dict(auth_kwargs or {})
        self.max_connections = max_connections
        self.ping_interval = ping_interval
        self.max_outstanding_pings = max_outstanding_pings
        self.reconnect_time_wait = reconnect_time_wait
        self.max_reconnect_attempts = max_reconnect_attempts
        self.service = service
        self.name = name
        self.connect_timeout = connect_timeout
        self.drain_timeout = drain_timeout

        self._connections: list[Any] = []
        # Numbers (1-based) of the connections nats-py reported closed for good, and how many
        # errors it has reported across the pool; both are read by `get_stats`.
        self._closed_for_good: set[int] = set()
        # Requests waiting for a reply: nats-py's drain stops a connection's reply subscription, so
        # a reply still on its way would be lost, and `close()` waits for these itself.
        self._requests_in_flight = 0
        self._requests_done = asyncio.Event()
        self._requests_done.set()
        self._error_count = 0
        # Set while `close()` closes the pool: nats-py calls `closed_cb` for a close the caller
        # asked for as well as for one it forced, and only the second is worth a warning.
        self._closing = False
        self._current_index = 0
        # Held while `connect()` opens the pool, so a second caller waits for the first and then
        # finds the pool made, instead of opening a second set beside it.
        self._connect_lock = asyncio.Lock()

    def _ping_kwargs(self) -> dict[str, Any]:
        """The ping settings that are set; one that is None leaves nats-py's default in force."""
        return {
            name: value
            for name, value in (
                ("ping_interval", self.ping_interval),
                ("max_outstanding_pings", self.max_outstanding_pings),
            )
            if value is not None
        }

    @property
    def _log(self) -> Any:
        """The loguru logger bound to the service this pool belongs to, when it was given a name."""
        return logger.bind(service=self.name) if self.name else logger

    async def connect(self) -> None:
        """Create optimized connection pool.

        A second call while the pool holds connections does nothing: the pool is never larger
        than ``max_connections``, including when two tasks call at once, and the second waits
        for the first. A call that fails, is cancelled or is cut off by the caller's timeout
        closes the connections it had opened and leaves the pool empty, so calling again
        connects afresh. After ``close()`` it connects afresh.
        """
        async with self._connect_lock:
            await self._connect()

    async def _connect(self) -> None:
        if self._connections:
            self._log.debug("Connection pool is already connected; connect() is a no-op")
            return
        self._closing = False
        self._log.info(
            f"Creating optimized NATS connection pool with {self.max_connections} connections"
        )

        for i in range(self.max_connections):
            try:
                try:
                    conn = await dial.connect(
                        self.nats_url,
                        timeout=self.connect_timeout,
                        **self.auth_kwargs,
                        **({"name": f"{self.name}-pool-{i + 1}"} if self.name else {}),
                        error_cb=self._error_callback(i + 1),
                        closed_cb=self._closed_callback(i + 1),
                        **self._ping_kwargs(),
                        reconnect_time_wait=self.reconnect_time_wait,
                        max_reconnect_attempts=self.max_reconnect_attempts,
                    )
                except builtins.TimeoutError as exc:
                    if self.connect_timeout is None:
                        raise
                    raise NatsError(
                        f"no answer within connect_timeout={self.connect_timeout}s"
                    ) from exc
                self._connections.append(conn)
                self._log.debug(f"Created optimized connection {i + 1}/{self.max_connections}")

            except BaseException as e:
                if isinstance(e, Exception):
                    self._log.error(f"Failed to create connection {i + 1}: {e}")
                # The ones already open are not left for a caller that may never reach `close()`:
                # a cancel or a timeout from the caller ends the loop the same way a failure does.
                await self._shutdown(drain=False)
                raise

        self._log.info(f"Optimized connection pool ready with {len(self._connections)} connections")

    def _error_callback(self, number: int) -> Any:
        async def on_error(exc: Exception) -> None:
            self._error_count += 1
            self._log.error(f"Pooled connection {number} reported an error: {exc!r}")

        return on_error

    def _closed_callback(self, number: int) -> Any:
        async def on_closed() -> None:
            if self._closing:
                return
            self._closed_for_good.add(number)
            self._log.warning(
                f"Pooled connection {number} is closed and will not reconnect; the pool "
                f"no longer hands it out"
            )

        return on_closed

    async def get_connection(self) -> Any:
        """Get the next open connection from the pool (round-robin).

        A connection that has closed for good, because its reconnect attempts ran out, is
        skipped, so one dead socket does not fail every Nth call for the life of the process.
        When every connection is closed the call raises, naming that.
        """
        if not self._connections:
            raise RuntimeError("No connections available - call connect() first")

        # No await between reading and advancing the index, so no other task can run between
        # them and nothing needs to guard it.
        for _ in range(len(self._connections)):
            conn = self._connections[self._current_index]
            self._current_index = (self._current_index + 1) % len(self._connections)
            if getattr(conn, "is_closed", False) is not True:
                return conn
        raise RuntimeError(
            f"Every one of the pool's {len(self._connections)} connections is closed; "
            f"close() and connect() the pool again"
        )

    async def request(
        self,
        subject: str,
        payload: bytes,
        timeout: float = 5.0,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Optimized request with connection pooling.

        Refused once ``close()`` has begun; a request already waiting for its reply is waited
        for by ``close()``. ``headers`` go out with the message as given.
        """
        self._refuse_while_closing()
        conn = await self.get_connection()
        self._requests_in_flight += 1
        self._requests_done.clear()
        try:
            if headers is None:
                return await conn.request(subject, payload, timeout=timeout)
            return await conn.request(subject, payload, timeout=timeout, headers=headers)
        finally:
            self._requests_in_flight -= 1
            if not self._requests_in_flight:
                self._requests_done.set()

    async def publish(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> None:
        """Optimized publish with connection pooling. Refused once ``close()`` has begun.

        ``headers`` go out with the message as given.
        """
        self._refuse_while_closing()
        conn = await self.get_connection()
        if headers is None:
            await conn.publish(subject, payload)
        else:
            await conn.publish(subject, payload, headers=headers)

    def _refuse_while_closing(self) -> None:
        if self._closing:
            raise RuntimeError("The connection pool is closing and takes no new requests")

    async def subscribe(self, subject: str, queue: str | None = None, cb: Any = None) -> Any:
        """Subscribe on the pool's first connection.

        Every subscription uses the same connection, whatever the rotation of `request` and
        `publish`, so the messages of different subscriptions keep the order the broker sent
        them in. A subscription is not spread across the pool, and it does not move if that
        connection closes for good.
        """
        if not self._connections:
            raise RuntimeError("No connections available")
        conn = self._connections[0]
        return await conn.subscribe(subject, queue=queue, cb=cb)

    async def close(self) -> None:
        """Wait for the requests in flight, then drain and close all connections in the pool.

        No new request or publish is taken once this begins. The requests already waiting for a
        reply are waited for, then each connection is drained, which flushes what it had been
        asked to publish, and the connections drain at the same time, so a pool of N waits for
        the slowest and not for N in turn. Both steps share ``drain_timeout``: the call returns
        within it, apart from closing what is left. A connection that has not drained by then is
        closed with what it still holds, and says so.
        """
        await self._shutdown(drain=True)

    async def _shutdown(self, *, drain: bool) -> None:
        self._log.info("Closing optimized connection pool")
        self._closing = True
        loop = asyncio.get_running_loop()
        deadline = None if self.drain_timeout is None else loop.time() + self.drain_timeout

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - loop.time())

        if drain and self._requests_in_flight:
            try:
                await asyncio.wait_for(self._requests_done.wait(), remaining())
            except TimeoutError:
                self._log.warning(
                    f"{self._requests_in_flight} request(s) still waiting for a reply after "
                    f"{self.drain_timeout}s; closing the pool anyway"
                )

        async def drain_then_close(number: int, conn: Any) -> None:
            if getattr(conn, "is_closed", False) is True:
                return
            try:
                if not drain:
                    raise _NoDrain
                await asyncio.wait_for(conn.drain(), remaining())
                self._log.debug(f"Drained and closed connection {number}")
                return
            except _NoDrain:
                pass
            except Exception as e:
                self._log.warning(
                    f"Connection {number} did not drain ({type(e).__name__}: {e}); closing it"
                )
            try:
                await conn.close()
            except Exception as e:
                self._log.warning(f"Error closing connection {number}: {e}")

        await asyncio.gather(*(drain_then_close(i + 1, c) for i, c in enumerate(self._connections)))

        self._connections.clear()
        self._closed_for_good.clear()
        self._log.info("Optimized connection pool closed")

    @property
    def is_connected(self) -> bool:
        """Whether any of the pool's own connections is connected.

        The pool is a separate set of sockets, so this reads them and nothing else: a service that
        has lost its own connection does not make a working pool read down. That state is
        `service_connected`.
        """
        return any(getattr(conn, "is_connected", False) for conn in self._connections)

    @property
    def service_connected(self) -> bool | None:
        """Whether the service that owns the pool is connected to the broker, or None.

        None when the pool has no service or the service has no such flag, so a pool built on its
        own does not claim to know.
        """
        if self.service is None or not hasattr(self.service, "is_broker_connected"):
            return None
        return bool(self.service.is_broker_connected)

    def get_stats(self) -> dict[str, Any]:
        """Get connection pool statistics.

        `active_connections` counts the pool's own connected sockets; `service_connected` is the
        owning service's connection, beside it and never mixed into it.
        """
        active_connections = sum(
            1 for conn in self._connections if getattr(conn, "is_connected", False)
        )

        return {
            "total_connections": len(self._connections),
            "active_connections": active_connections,
            "service_connected": self.service_connected,
            "closed_connections": len(self._closed_for_good),
            "connection_errors": self._error_count,
            "max_connections": self.max_connections,
            "current_index": self._current_index,
            "utilization_percent": (active_connections / self.max_connections) * 100
            if self.max_connections > 0
            else 0,
        }

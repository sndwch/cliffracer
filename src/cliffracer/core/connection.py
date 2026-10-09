"""The NATS and JetStream broker connection manager.

Governs network transport connection lifecycles, broker state transitions,
credential redaction, subscription tracking, and reconnect callbacks.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

import nats
from loguru import logger as global_logger
from nats.errors import Error as NatsError
from nats.js import JetStreamContext

from . import dial
from .endpoints import redact_nats_url
from .lifecycle import bounded_shutdown_timeout
from .service_config import ServiceConfig

_CLOSED_STOP_TIMEOUT = 10.0


async def flush_through_buffered_commands(nc: Any) -> None:
    """Return once the broker has processed every command sent on `nc` before this call.

    `nc.flush()` is meant to do this, and does not when it is called straight after `subscribe`:
    nats-py writes the PING to the socket at once, but a SUB goes into a pending buffer that
    another task writes out, so the PING can reach the broker AHEAD of the SUB it should
    confirm. The PONG then proves nothing about it, and a request from another connection can
    arrive before the broker has read the SUB. Measured with nats-py alone, on a loaded host, at
    about 4% of subscribe-then-flush-then-request rounds.

    The first flush gives the pending commands their turn to be written; the second flush's PING
    is written after them, on the same socket, so its PONG is the broker saying it has read them.
    """
    await nc.flush()
    await nc.flush()


class BrokerConnectionState(Enum):
    """The transport connection status between the service and NATS."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DRAINING = "draining"
    CLOSED = "closed"


class ConnectionManager:
    """Manages the lifecycle of NATS and JetStream transport connections.

    Invariants:
    - Binds initial connection within ``connect_timeout`` or raises NatsError.
    - Instantiates JetStream context only if ``config.jetstream_enabled`` is True.
    - Never executes message dispatch or handler logic.
    - Tracks active subscription listener tasks in ``subscriptions``.
    """

    def __init__(
        self,
        config: ServiceConfig,
        logger: Any = None,
        on_closed_handler: Callable[[], Awaitable[None]] | None = None,
        is_running_fn: Callable[[], bool] | None = None,
        logger_provider: Callable[[], Any] | None = None,
        on_connection_lost: Callable[[], Awaitable[None]] | None = None,
        on_connection_regained: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self._logger = logger
        self._logger_provider = logger_provider
        self.on_closed_handler = on_closed_handler
        #: What the container runs, before the config slot, when the client reports the
        #: connection lost or regained: the extensions' `on_disconnect` / `on_reconnect`.
        self.on_connection_lost = on_connection_lost
        self.on_connection_regained = on_connection_regained
        self.is_running_fn = is_running_fn

        self.nc: nats.NATS | None = None
        self.js: JetStreamContext | None = None
        #: How `connect` opens the connection. None, the default, is `dial.connect`, looked up
        #: when `connect` runs, which dials `config.nats_url`. `ServiceTestHarness(broker=...)`
        #: sets the broker's own `connect`, so a service under test starts over an in-process
        #: bus; nothing else sets it.
        self.dial: Callable[..., Awaitable[Any]] | None = None
        self.subscriptions: set[asyncio.Task[Any]] = set()
        self._subscription_handles: dict[int, Any] = {}

    @property
    def logger(self) -> Any:
        if self._logger_provider is not None:
            return self._logger_provider()
        return self._logger or global_logger.bind(service=self.config.name)

    @logger.setter
    def logger(self, value: Any) -> None:
        self._logger = value

    @property
    def broker_state(self) -> BrokerConnectionState:
        """The current NATS transport connection state."""
        if not self.nc:
            return BrokerConnectionState.DISCONNECTED
        if getattr(self.nc, "is_closed", False):
            return BrokerConnectionState.CLOSED
        if getattr(self.nc, "is_draining", False):
            return BrokerConnectionState.DRAINING
        if getattr(self.nc, "is_connected", False):
            return BrokerConnectionState.CONNECTED
        if getattr(self.nc, "is_connecting", False) or getattr(self.nc, "is_reconnecting", False):
            return BrokerConnectionState.CONNECTING
        return BrokerConnectionState.DISCONNECTED

    @property
    def is_broker_connected(self) -> bool:
        """Indicates whether the underlying NATS connection is in CONNECTED state."""
        return self.broker_state == BrokerConnectionState.CONNECTED

    @property
    def is_connected(self) -> bool:
        """Alias for is_broker_connected."""
        return self.is_broker_connected

    @property
    def jetstream_active(self) -> bool:
        """Indicates whether JetStream publishing and subscription paths are enabled."""
        return self.config.jetstream_enabled and self.js is not None

    async def connect(self) -> None:
        """Establish transport connection to the configured NATS broker.

        Applies credentials and connection bounds. On permanent failure,
        raises NatsError.
        """
        auth_kwargs = self.config.nats_connect_kwargs()
        # Passed only when set: unset must leave nats-py's own defaults in force.
        ping_kwargs = {
            name: value
            for name in ("ping_interval", "max_outstanding_pings")
            if (value := getattr(self.config, name)) is not None
        }
        try:
            try:
                self.nc = await (self.dial or dial.connect)(
                    self.config.nats_url,
                    timeout=self.config.connect_timeout,
                    name=self.config.name,
                    max_reconnect_attempts=self.config.max_reconnect_attempts,
                    reconnect_time_wait=self.config.reconnect_time_wait,
                    error_cb=self._error_callback,
                    disconnected_cb=self._disconnected_callback,
                    reconnected_cb=self._reconnected_callback,
                    closed_cb=self._closed_callback,
                    **ping_kwargs,
                    **auth_kwargs,
                )
            except builtins.TimeoutError as exc:
                raise NatsError(
                    f"no answer within connect_timeout={self.config.connect_timeout}s"
                ) from exc
        except NatsError as exc:
            self.logger.error(
                f"Service '{self.config.name}' could not reach NATS at "
                f"{redact_nats_url(self.config.nats_url)}: {exc}"
            )
            raise

        if self.config.jetstream_enabled:
            self.js = self.nc.jetstream()

        self.logger.info(
            f"Service '{self.config.name}' connected to NATS at "
            f"{redact_nats_url(self.config.nats_url)}"
        )

        if self.config.on_connect is not None:
            await self._maybe_await(self.config.on_connect())

    async def disconnect(self) -> None:
        """Drain and close the NATS connection."""
        if self.nc and self.broker_state != BrokerConnectionState.CLOSED:
            try:
                is_connecting = getattr(self.nc, "is_connecting", False) is True
                is_reconnecting = getattr(self.nc, "is_reconnecting", False) is True
                if not (is_connecting or is_reconnecting):
                    try:
                        # Bounded by `shutdown_timeout`: the drain flushes and waits on a PONG, and
                        # a path to the broker that drops packets still reads as connected, so
                        # nothing else would end the wait before nats-py's own 10 seconds.
                        await asyncio.wait_for(
                            self.nc.drain(),
                            timeout=bounded_shutdown_timeout(
                                getattr(self.config, "shutdown_timeout", None),
                                self.logger,
                                "Draining the broker connection",
                            ),
                        )
                    except (
                        nats.errors.ConnectionReconnectingError,
                        nats.errors.ConnectionClosedError,
                    ):
                        pass
                    except builtins.TimeoutError as exc:
                        # `FlushTimeoutError` is one, and so is the deadline above. The broker did
                        # not answer, so what was buffered may not have been sent; the connection
                        # is closed below all the same, and the stop is not a failed one.
                        self.logger.warning(
                            f"Service '{self.config.name}' could not drain its NATS connection "
                            f"before it was closed ({type(exc).__name__}): the broker did not "
                            f"answer, so messages still buffered may not have been sent"
                        )
            finally:
                try:
                    await self.nc.close()
                except Exception:
                    pass

    def track_subscription(self, sub: Any) -> None:
        """Own a broker subscription before any listener task or further startup await."""
        self._subscription_handles[id(sub)] = sub

    async def unsubscribe(self, sub: Any) -> None:
        """Release an owned subscription once, independently of its listener task."""
        if self._subscription_handles.pop(id(sub), None) is None:
            return
        if (
            self.nc
            and self.broker_state != BrokerConnectionState.CLOSED
            and not getattr(self.nc, "is_draining", False)
        ):
            try:
                await sub.unsubscribe()
            except Exception:
                pass

    async def unsubscribe_all(self) -> None:
        """Cancel listeners and close intake even when a listener never began running."""
        tasks = list(self.subscriptions)
        try:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.subscriptions.difference_update(tasks)
            await asyncio.gather(
                *(self.unsubscribe(sub) for sub in list(self._subscription_handles.values()))
            )

    async def _error_callback(self, e: Any) -> None:
        self.logger.error(f"NATS error: {e}")
        if self.config.on_error is not None:
            await self._maybe_await(self.config.on_error(e))

    async def _disconnected_callback(self) -> None:
        self.logger.warning(f"Service '{self.config.name}' disconnected from NATS")
        if self.on_connection_lost is not None:
            await self.on_connection_lost()
        if self.config.on_disconnect is not None:
            await self._maybe_await(self.config.on_disconnect())

    async def _reconnected_callback(self) -> None:
        self.logger.info(f"Service '{self.config.name}' reconnected to NATS")
        if self.on_connection_regained is not None:
            await self.on_connection_regained()
        if self.config.on_connect is not None:
            await self._maybe_await(self.config.on_connect())

    async def _closed_callback(self) -> None:
        """Handle terminal broker connection closure."""
        is_running = self.is_running_fn() if self.is_running_fn else True
        if not is_running:
            self.logger.info(f"Service '{self.config.name}' connection closed")
            return

        self.logger.error(
            f"Service '{self.config.name}' connection to NATS is CLOSED and will "
            f"not be retried. The service cannot send or receive anything."
        )

        if not self.config.exit_on_closed:
            self.logger.error(
                f"Service '{self.config.name}' staying up: exit_on_closed is False. "
                f"health_check() reports 'disconnected'."
            )
            return

        stopped = True
        try:
            if self.on_closed_handler is not None:
                await asyncio.wait_for(self.on_closed_handler(), timeout=_CLOSED_STOP_TIMEOUT)
        except TimeoutError:
            # A TimeoutError has no message, so formatting it says nothing at all.
            stopped = False
            self.logger.warning(
                f"Service '{self.config.name}' could not stop cleanly on connection close: "
                f"the stop did not finish within {_CLOSED_STOP_TIMEOUT:g} seconds, which is "
                f"fixed and not governed by shutdown_timeout, and was cancelled part-way, so "
                f"steps it had not reached may not have run. on_shutdown still runs after the "
                f"cut-off, for up to shutdown_timeout"
            )
        except Exception as exc:
            stopped = False
            self.logger.warning(
                f"Service '{self.config.name}' could not stop cleanly on connection close: {exc!r}"
            )

        if stopped:
            self.logger.error(f"Service '{self.config.name}' stopped after NATS connection closed.")

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

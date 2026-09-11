"""The NATS and JetStream broker connection manager.

Governs network transport connection lifecycles, broker state transitions,
credential redaction, subscription tracking, and reconnect callbacks.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
import re
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

import nats
from loguru import logger as global_logger
from nats.errors import Error as NatsError
from nats.js import JetStreamContext

from .service_config import ServiceConfig

_NATS_SCHEME_RE = re.compile(r"^(nats|tls|ws|wss)://", re.IGNORECASE)

_CLOSED_STOP_TIMEOUT = 10.0


class BrokerConnectionState(Enum):
    """The transport connection status between the service and NATS."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DRAINING = "draining"
    CLOSED = "closed"


def redact_nats_url(url: str) -> str:
    """Strip credentials from a NATS URL for diagnostic logging.

    Invariants:
    - Returns string unchanged if no "@" delimiter is present.
    - Matches only allowed schemes: nats, tls, ws, wss.
    - Never raises an exception; returns fallback string on parsing error.
    """
    try:
        text = str(url)
        if "@" not in text:
            return text
        match = _NATS_SCHEME_RE.match(text)
        scheme = match.group(1) if match else ""
        rest = text[match.end() :] if match else text
        _, _, hostpart = rest.rpartition("@")
        return f"{scheme}://***@{hostpart}" if scheme else f"***@{hostpart}"
    except Exception:
        return "<unparseable nats url>"


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
    ) -> None:
        self.config = config
        self._logger = logger
        self._logger_provider = logger_provider
        self.on_closed_handler = on_closed_handler
        self.is_running_fn = is_running_fn

        self.nc: nats.NATS | None = None
        self.js: JetStreamContext | None = None
        self.subscriptions: set[asyncio.Task[Any]] = set()

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
        auth_kwargs = self.config.nats_auth_kwargs()
        connect_coro = nats.connect(
            self.config.nats_url,
            name=self.config.name,
            max_reconnect_attempts=self.config.max_reconnect_attempts,
            reconnect_time_wait=self.config.reconnect_time_wait,
            error_cb=self._error_callback,
            disconnected_cb=self._disconnected_callback,
            reconnected_cb=self._reconnected_callback,
            closed_cb=self._closed_callback,
            **auth_kwargs,
        )

        try:
            try:
                if self.config.connect_timeout is None:
                    self.nc = await connect_coro
                else:
                    self.nc = await asyncio.wait_for(connect_coro, self.config.connect_timeout)
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
                        await self.nc.drain()
                    except (
                        nats.errors.ConnectionReconnectingError,
                        nats.errors.ConnectionClosedError,
                    ):
                        pass
            finally:
                try:
                    await self.nc.close()
                except Exception:
                    pass

    async def unsubscribe_all(self) -> None:
        """Cancel and drain all tracked subscription listener tasks."""
        if not self.subscriptions:
            return
        for task in list(self.subscriptions):
            task.cancel()
        await asyncio.gather(*list(self.subscriptions), return_exceptions=True)
        self.subscriptions.clear()

    async def _error_callback(self, e: Any) -> None:
        self.logger.error(f"NATS error: {e}")
        if self.config.on_error is not None:
            await self._maybe_await(self.config.on_error(e))

    async def _disconnected_callback(self) -> None:
        self.logger.warning(f"Service '{self.config.name}' disconnected from NATS")
        if self.config.on_disconnect is not None:
            await self._maybe_await(self.config.on_disconnect())

    async def _reconnected_callback(self) -> None:
        self.logger.info(f"Service '{self.config.name}' reconnected to NATS")
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

        try:
            if self.on_closed_handler is not None:
                await asyncio.wait_for(self.on_closed_handler(), timeout=_CLOSED_STOP_TIMEOUT)
        except Exception as exc:
            self.logger.warning(
                f"Service '{self.config.name}' could not stop cleanly on connection close: {exc}"
            )

        self.logger.error(f"Service '{self.config.name}' stopped after NATS connection closed.")

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

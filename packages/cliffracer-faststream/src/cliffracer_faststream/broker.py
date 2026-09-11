"""Cliffracer-hosted FastStream NATS broker."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from faststream._internal.broker.broker import BrokerUsecase
from faststream.middlewares import AckPolicy
from faststream.nats.broker.broker import NatsBroker

if TYPE_CHECKING:
    from fast_depends.dependencies import Dependant
    from faststream.types import BrokerMiddleware


class CliffracerHostedNatsBroker(NatsBroker):
    """Hosted FastStream NATS broker adapter.

    Invariants:
    - Never opens its own TCP socket; uses the connection attached by Cliffracer.
    - Disables autonomous FastStream stream declaration (stream.declare = False).
    - Stops subscriber consumer loops on stop(), but strictly avoids self._connection.drain().
    - Enforces AckPolicy.MANUAL across all mounted subscribers.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._attached: bool = False
        self._cliffracer_js: Any = None

    def attach_cliffracer_connection(self, nc: Any, js: Any = None) -> None:
        """Attach active Cliffracer NATS client and JetStream context."""
        if nc is None or not getattr(nc, "is_connected", False):
            raise RuntimeError("Cannot attach uninitialized or disconnected NATS client")
        self._connection = nc
        self._cliffracer_js = js
        self.config.connect(nc)
        self._setup_logger()
        self._attached = True

        self._suppress_stream_declarations()
        self.enforce_manual_ack()

    def _suppress_stream_declarations(self) -> None:
        """Suppress FastStream autonomous stream mutation/declaration."""
        stream_builder = getattr(self, "_stream_builder", None)
        if stream_builder is not None and hasattr(stream_builder, "objects"):
            for stream_obj in stream_builder.objects.values():
                if isinstance(stream_obj, tuple | list) and len(stream_obj) > 0:
                    stream_obj[0].declare = False

    def enforce_manual_ack(self) -> None:
        """Enforce AckPolicy.MANUAL and disable auto ack across all subscribers."""
        for sub in getattr(self, "subscribers", ()):
            sub.ack_policy = AckPolicy.MANUAL
            sub._SubscriberUsecase__auto_ack_disabled = True

    def include_router(
        self,
        router: Any,
        *,
        prefix: str = "",
        dependencies: Iterable[Dependant] = (),
        middlewares: Sequence[BrokerMiddleware[Any, Any]] = (),
        include_in_schema: bool | None = None,
    ) -> None:
        """Include a router and suppress autonomous stream declaration on its routes."""
        super().include_router(
            router,
            prefix=prefix,
            dependencies=dependencies,
            middlewares=middlewares,
            include_in_schema=include_in_schema,
        )
        self._suppress_stream_declarations()
        self.enforce_manual_ack()

    def subscriber(self, *args: Any, **kwargs: Any) -> Any:
        """Register a subscriber, enforcing stream shielding and manual ack policy."""
        sub = super().subscriber(*args, **kwargs)
        self._suppress_stream_declarations()
        self.enforce_manual_ack()
        return sub

    async def connect(self) -> Any:
        """Bypass network dialing; return pre-attached connection."""
        if self._connection is None:
            raise RuntimeError(
                "CliffracerHostedNatsBroker requires an active connection attached "
                "by Cliffracer during start() lifecycle"
            )
        return self._connection

    async def stop(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: Any = None,
    ) -> None:
        """Stop subscriber consumer loops without draining the shared NATS transport."""
        await BrokerUsecase.stop(self, exc_type, exc_val, exc_tb)

        self.config.disconnect()
        # CRITICAL: Do NOT execute self._connection.drain()!
        # Detach connection reference cleanly
        self._connection = None
        self._attached = False

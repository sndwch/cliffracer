"""FastStream host extension for Cliffracer."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from cliffracer.core.exceptions import ServiceLifecycleError
from cliffracer.core.extension import Extension, ExtensionSetupContext, SharedDependency

from .broker import CliffracerHostedNatsBroker
from .middleware import CliffracerAckMiddleware

if TYPE_CHECKING:
    from faststream.nats import NatsRouter


class FastStreamExtension(Extension):
    """Host extension mounting FastStream routers on Cliffracer.

    Invariants:
    - Defers NATS connection attachment to start() lifecycle (after container.connect()).
    - Uses CliffracerHostedNatsBroker to prevent premature transport drain on stop().
    - Injects CliffracerAckMiddleware and enforces AckPolicy.MANUAL to prevent data loss.
    - Halts incoming traffic via drain(timeout) during container shutdown Step 4.
    - Exposes Cliffracer service, container, config, KV, and Resilience into ContextRepo.
    - Reports running/stopped status and mounted routes via health_details() and info_details().
    """

    name: str = "faststream"

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        if "router" in kwargs and kwargs["router"] is not None:
            r = kwargs["router"]
            if not isinstance(r, SharedDependency):
                kwargs["router"] = SharedDependency(r)
        elif len(args) > 0 and args[0] is not None:
            r = args[0]
            if not isinstance(r, SharedDependency):
                args = (SharedDependency(r), *args[1:])
        return super().__new__(cls, *args, **kwargs)

    def __init__(
        self,
        router: NatsRouter
        | SharedDependency[NatsRouter]
        | Sequence[NatsRouter | SharedDependency[NatsRouter]]
        | None = None,
        *,
        broker: CliffracerHostedNatsBroker | None = None,
    ) -> None:
        self._init_routers: tuple[Any, ...]
        if router is None:
            self._init_routers = ()
        elif isinstance(router, SharedDependency):
            self._init_routers = (router.value,)
        elif isinstance(router, list | tuple):
            self._init_routers = tuple(
                r.value if isinstance(r, SharedDependency) else r for r in router
            )
        else:
            self._init_routers = (router,)

        self._explicit_broker = broker
        self._routers: list[Any] = []
        self.hosted_broker: CliffracerHostedNatsBroker | None = None

    def bind(self, service: Any, name: str) -> FastStreamExtension:
        """Bind to service and return typed FastStreamExtension."""
        return cast(FastStreamExtension, super().bind(service, name))

    def include_router(self, router: Any) -> None:
        """Mount an additional FastStream NatsRouter."""
        self._routers.append(router)
        if self.hosted_broker is not None:
            self.hosted_broker.include_router(router)
            self.hosted_broker.enforce_manual_ack()

    # -- lifecycle -------------------------------------------------------------

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        """Prepare hosted broker and mount routers before NATS connects.

        Invariants:
        - Must NOT access ctx.service.container.nc (not yet connected).
        - Enforces AckPolicy.MANUAL and registers CliffracerAckMiddleware.
        """
        if self.service is None and ctx is not None and hasattr(ctx, "service"):
            self.service = ctx.service

        if self.hosted_broker is None:
            self.hosted_broker = self._explicit_broker or CliffracerHostedNatsBroker()

        self._routers = list(self._init_routers)
        for r in self._routers:
            self.hosted_broker.include_router(r)

        # Register CliffracerAckMiddleware
        self.hosted_broker.add_middleware(CliffracerAckMiddleware)
        # Enforce manual ack across all mounted subscribers
        self.hosted_broker.enforce_manual_ack()

    async def start(self) -> None:
        """Attach active Cliffracer connection, surface context dependencies, and start broker."""
        if self.hosted_broker is None:
            self.hosted_broker = self._explicit_broker or CliffracerHostedNatsBroker()

        container = getattr(self.service, "container", None)
        nc = getattr(container, "nc", None)
        if nc is None or not getattr(nc, "is_connected", False):
            raise ServiceLifecycleError(
                f"Cannot start extension '{self.name}': NATS connection is uninitialized or disconnected"
            )

        js = getattr(container, "js", None)
        self.hosted_broker.attach_cliffracer_connection(nc, js)

        # Surface Cliffracer primitives into FastStream ContextRepo
        context = self.hosted_broker.context
        context.set_global("service", self.service)
        if container is not None:
            context.set_global("container", container)
            for ext in getattr(container, "_extensions", []):
                ext_name = getattr(ext, "name", None)
                if ext_name:
                    context.set_global(ext_name, ext)

        config = getattr(self.service, "config", None)
        if config is not None:
            context.set_global("config", config)

        if hasattr(self.service, "kv"):
            context.set_global("kv", self.service.kv)
        if hasattr(self.service, "resilience"):
            context.set_global("resilience", self.service.resilience)

        # Start hosted broker and subscriber consumption loops
        await self.hosted_broker.start()

    async def drain(self, timeout: float = 30.0) -> None:
        """Halt incoming messages by unsubscribing all subscriber consumers."""
        if self.hosted_broker is None:
            return

        for sub in getattr(self.hosted_broker, "subscribers", ()):
            # Unsubscribe main NATS / JetStream subscription
            sub_obj = getattr(sub, "subscription", None)
            if (
                sub_obj is not None
                and hasattr(sub_obj, "unsubscribe")
                and callable(sub_obj.unsubscribe)
            ):
                try:
                    await sub_obj.unsubscribe()
                except Exception as exc:
                    logger.debug("Error unsubscribing during drain: {}", exc)

            # Unsubscribe pull consumer fetch subscription if applicable
            fetch_sub = getattr(sub, "_fetch_sub", None)
            if (
                fetch_sub is not None
                and hasattr(fetch_sub, "unsubscribe")
                and callable(fetch_sub.unsubscribe)
            ):
                try:
                    await fetch_sub.unsubscribe()
                except Exception as exc:
                    logger.debug("Error unsubscribing fetch subscription during drain: {}", exc)

            if hasattr(sub, "stop") and callable(sub.stop):
                try:
                    await sub.stop()
                except Exception as exc:
                    logger.debug("Error stopping subscriber during drain: {}", exc)

            sub.running = False

    async def stop(self) -> None:
        """Stop subscriber loops without draining the underlying NATS connection."""
        if self.hosted_broker is not None:
            await self.hosted_broker.stop()
            # Clean up context globals
            context = self.hosted_broker.context
            for key in ("service", "container", "config", "kv", "resilience"):
                try:
                    context.reset_global(key)
                except Exception:
                    pass
            container = getattr(self.service, "container", None)
            if container is not None:
                for ext in getattr(container, "_extensions", []):
                    ext_name = getattr(ext, "name", None)
                    if ext_name:
                        try:
                            context.reset_global(ext_name)
                        except Exception:
                            pass

    # -- telemetry & introspection ---------------------------------------------

    def health_details(self) -> dict[str, Any] | None:
        """Return operational health details for GET /health endpoint."""
        if self.hosted_broker is None:
            return {"status": "uninitialized"}

        is_running = getattr(self.hosted_broker, "running", False)
        subscribers = list(getattr(self.hosted_broker, "subscribers", []))
        publishers = list(getattr(self.hosted_broker, "publishers", []))

        active_routes: list[str] = []
        for sub in subscribers:
            subj = getattr(sub, "subject", "")
            route_str = getattr(subj, "template", str(subj)) if subj is not None else ""
            active_routes.append(route_str)

        return {
            "status": "running" if is_running else "stopped",
            "subscribers_count": len(subscribers),
            "publishers_count": len(publishers),
            "active_routes": active_routes,
        }

    def info_details(self) -> dict[str, Any] | None:
        """Return introspection details for GET /info endpoint."""
        if self.hosted_broker is None:
            return None

        subscribers_info = []
        for sub in getattr(self.hosted_broker, "subscribers", []):
            subj = getattr(sub, "subject", "")
            route_str = getattr(subj, "template", str(subj)) if subj is not None else ""
            stream_obj = getattr(sub, "stream", None)
            stream_name = getattr(stream_obj, "name", str(stream_obj)) if stream_obj else None
            subscribers_info.append(
                {
                    "subject": route_str,
                    "queue": getattr(sub, "queue", None),
                    "stream": stream_name,
                }
            )

        return {
            "routers_count": len(self._routers),
            "subscribers": subscribers_info,
        }

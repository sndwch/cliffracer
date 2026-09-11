"""HTTP and WebSocket server extension."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import MappingProxyType
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from loguru import logger

from cliffracer.core.correlation import with_correlation_id
from cliffracer.core.extension import Extension, ExtensionSetupContext, entrypoint

from .config import HttpConfig
from .correlation_middleware import CorrelationMiddleware


class HttpExtension(Extension):
    """FastAPI and WebSocket server extension.

    Binds declared HTTP routes and WebSocket endpoints onto an internal
    FastAPI application served by Uvicorn during the service lifecycle.
    """

    def __init__(self, host: str | None = None, port: int | None = None, **fastapi_kwargs: Any):
        env = HttpConfig()
        self.host = host if host is not None else env.host
        self.port = port if port is not None else env.port
        # Store fastapi_kwargs in an immutable mapping view.
        self._fastapi_kwargs = MappingProxyType(dict(fastapi_kwargs))

        # Instance state initialized in setup() for per-service isolation.
        self.app: FastAPI | None = None
        self.active_connections: set[WebSocket] | None = None
        self._websocket_handlers: dict[str, Callable] | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    # -- markers (used at class-body time on the UNBOUND instance) -------------
    def get(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return entrypoint("http_route", owner=self, method="GET", path=path, kwargs=kw)

    def post(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return entrypoint("http_route", owner=self, method="POST", path=path, kwargs=kw)

    def put(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return entrypoint("http_route", owner=self, method="PUT", path=path, kwargs=kw)

    def delete(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return entrypoint("http_route", owner=self, method="DELETE", path=path, kwargs=kw)

    def websocket(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return entrypoint("http_websocket", owner=self, path=path)

    def entrypoint_kinds(self) -> dict[str, Callable[..., Any]]:
        return {"http_route": self._bind_route, "http_websocket": self._bind_websocket}

    # -- lifecycle -------------------------------------------------------------
    async def setup(self, ctx: ExtensionSetupContext) -> None:
        service = ctx.service
        self.active_connections = set()
        self._websocket_handlers = {}

        # Explicit title overrides the default service API title.
        fastapi_kwargs = {"title": f"{ctx.service_config.name} API", **self._fastapi_kwargs}
        self.app = FastAPI(**fastapi_kwargs)
        self.app.add_middleware(CorrelationMiddleware)

        @self.app.get("/live")
        @with_correlation_id
        async def live() -> JSONResponse:
            if hasattr(service, "liveness_check"):
                data = service.liveness_check()
                if asyncio.iscoroutine(data):
                    data = await data
            elif hasattr(service, "is_live"):
                data = service.is_live()
                if asyncio.iscoroutine(data):
                    data = await data
            else:
                running = getattr(service, "_running", True)
                service_name = getattr(getattr(service, "config", None), "name", "service")
                data = {
                    "service": service_name,
                    "status": "healthy" if running else "stopped",
                }
            return JSONResponse(
                content=jsonable_encoder(data),
                status_code=200 if data.get("status") == "healthy" else 503,
            )

        @self.app.get("/ready")
        @with_correlation_id
        async def ready() -> JSONResponse:
            body = await service.health_check()
            return JSONResponse(
                content=jsonable_encoder(body),
                status_code=200 if body.get("status") == "healthy" else 503,
            )

        @self.app.get("/health")
        @with_correlation_id
        async def health() -> JSONResponse:
            body = await service.health_check()
            return JSONResponse(
                content=jsonable_encoder(body),
                status_code=200 if body.get("status") == "healthy" else 503,
            )

        @self.app.get("/info")
        @with_correlation_id
        async def info() -> dict[str, Any]:
            return cast(dict[str, Any], service.get_service_info())

        # Disable the core health listener since HttpExtension serves health endpoints directly.
        service.health_listener.disable(
            f"cliffracer_http serves health endpoints on port {self.port}"
        )

    async def start(self) -> None:
        assert self.app is not None
        config = uvicorn.Config(app=self.app, host=self.host, port=self.port, log_level="info")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        logger.info(f"HTTP server started on http://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self.active_connections is not None:
            import asyncio

            for ws in list(self.active_connections):
                try:
                    await ws.close(code=1001, reason="Server shutting down")
                except Exception:
                    pass

        if self._server is not None:
            self._server.should_exit = True
            try:
                import asyncio

                if self._task is not None:
                    await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                logger.warning("HTTP server shutdown timed out")
            self._server = None
            logger.info("HTTP server stopped")

    # -- binders ---------------------------------------------------------------
    def _bind_route(
        self,
        service: Any,
        method_name: str,
        bound: Callable[..., Any],
        spec: dict[str, Any],
    ) -> None:
        assert self.app is not None
        self.app.add_api_route(spec["path"], bound, methods=[spec["method"]], **spec["kwargs"])
        logger.debug(f"Registered HTTP route {spec['method']} {spec['path']} -> {method_name}")

    def _bind_websocket(
        self,
        service: Any,
        method_name: str,
        bound: Callable[..., Any],
        spec: dict[str, Any],
    ) -> None:
        assert self._websocket_handlers is not None
        self._websocket_handlers[spec["path"]] = bound
        assert self.app is not None

        @self.app.websocket(spec["path"])
        async def endpoint(websocket: WebSocket) -> None:
            assert self.active_connections is not None
            self.active_connections.add(websocket)
            try:
                await bound(websocket)
            except WebSocketDisconnect:
                logger.info("WebSocket client disconnected")
            finally:
                self.active_connections.discard(websocket)

    # -- contributions ---------------------------------------------------------
    def health_details(self) -> dict[str, Any] | None:
        # No contribution before setup(): the containers do not exist yet, and
        # "not set up" is not an error worth reporting on /health.
        if self._websocket_handlers is None or self.active_connections is None:
            return None
        return {
            "websockets": {
                "active_connections": len(self.active_connections),
                "registered_handlers": len(self._websocket_handlers),
            }
        }

    def info_details(self) -> dict[str, Any] | None:
        if self._websocket_handlers is None:
            return None
        return {"websocket_handlers": list(self._websocket_handlers)}

    async def broadcast_to_websockets(self, message: dict[str, Any]) -> None:
        if self.active_connections is None:
            return
        text = json.dumps(message)
        dead: set[WebSocket] = set()
        for ws in list(self.active_connections):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001 - a dead socket must not stop the others
                dead.add(ws)
        self.active_connections -= dead

    def mount_rpc_service(
        self,
        target: Any,
        service: Any = None,
        prefix: str = "",
        verb_overrides: dict[str, str] | None = None,
        path_overrides: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        """Convenience helper to mount an RPC service's routes directly onto this HttpExtension's app."""
        from .gateway import mount_rpc_routes

        if self.app is None:
            raise RuntimeError(
                "HttpExtension.app is not initialized; mount_rpc_service must be called after setup()"
            )
        return mount_rpc_routes(
            app=self.app,
            service=service or self.service,
            target=target,
            prefix=prefix,
            verb_overrides=verb_overrides,
            path_overrides=path_overrides,
            **kwargs,
        )

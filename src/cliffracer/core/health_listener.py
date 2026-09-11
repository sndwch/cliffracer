"""GET /live, GET /ready, GET /health, and GET /info endpoints without a web framework.

A container healthcheck needs probes. This serves them with asyncio and
nothing else, so a service that wants no HTTP still answers its probes:
- /live: Process liveness probe. 200 when running, 503 when stopped. Never checks external dependencies.
- /ready: Readiness probe. 200 when healthy, 503 otherwise (broker disconnected or dependency failed).
- /health: Backward-compatible alias for /ready.
- /info: Service descriptor payload.
"""

from __future__ import annotations

import asyncio
import errno
import json
from typing import Any

from loguru import logger


class HealthListener:
    #: Tests set this to 0 so no test ever binds a fixed port on a shared runner.
    _test_port_override: int | None = None

    def __init__(self, service: Any, host: str, port: int, *, port_is_explicit: bool = True):
        self.service = service
        self.host = host
        self._requested_port = port
        # Whether the caller CHOSE this port or inherited the default. It
        # decides what a collision means: a chosen port in use is a
        # misconfiguration, an inherited one is just a second service in
        # the same process.
        self._port_is_explicit = port_is_explicit
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._disabled: str | None = None

    def disable(self, reason: str) -> None:
        """An extension that serves the same routes on the same port calls this."""
        self._disabled = reason

    async def start(self) -> None:
        if self._disabled:
            logger.info(f"health listener not started: {self._disabled}")
            return
        # Resolve health port at start rather than construction to reflect runtime configuration overrides.
        requested = self._requested_port
        config = getattr(self.service, "config", None)
        if config is not None and hasattr(config, "health_port"):
            requested = config.health_port
        port = requested if self._test_port_override is None else self._test_port_override
        try:
            self._server = await asyncio.start_server(self._handle, self.host, port)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                logger.error(
                    f"health listener failed to bind {self.host}:{port}: port already in use"
                )
            raise
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info(f"health listener on http://{self.host}:{self.port}/health")

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        self.port = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            while (await asyncio.wait_for(reader.readline(), timeout=5)) not in (
                b"\r\n",
                b"\n",
                b"",
            ):
                pass
            parts = request_line.decode(errors="replace").split()
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
            if method != "GET":
                await self._respond(writer, 405, {"error": "method not allowed"})
            elif path == "/live":
                if hasattr(self.service, "liveness_check"):
                    live_data = self.service.liveness_check()
                    if asyncio.iscoroutine(live_data):
                        live_data = await live_data
                elif hasattr(self.service, "is_live"):
                    live_data = self.service.is_live()
                    if asyncio.iscoroutine(live_data):
                        live_data = await live_data
                else:
                    running = getattr(self.service, "_running", True)
                    service_name = getattr(getattr(self.service, "config", None), "name", "service")
                    live_data = {
                        "service": service_name,
                        "status": "healthy" if running else "stopped",
                    }
                status_code = 200 if live_data.get("status") == "healthy" else 503
                await self._respond(writer, status_code, live_data)
            elif path in ("/ready", "/health"):
                body = await self.service.health_check()
                await self._respond(writer, 200 if body.get("status") == "healthy" else 503, body)
            elif path == "/info":
                await self._respond(writer, 200, self.service.get_service_info())
            else:
                await self._respond(writer, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 - the probe must get an answer
            logger.warning(f"health listener request failed: {exc}")
            try:
                await self._respond(writer, 500, {"error": str(exc)})
            except Exception:  # noqa: BLE001
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _respond(self, writer: asyncio.StreamWriter, status: int, body: dict) -> None:
        payload = json.dumps(body, default=str).encode()
        reason = {
            200: "OK",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }[status]
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()

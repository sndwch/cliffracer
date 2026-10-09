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

from .error_text import exception_text

# THE WHOLE REQUEST, not each line. The header read was `wait_for(readline(),
# timeout=5)` inside an unbounded loop, so the five seconds reset on every line:
# a client writing one short header every couple of seconds kept its handler
# alive for as long as it cared to, and `stop()` waits on live handlers.
REQUEST_DEADLINE_SECONDS = 5.0

# A cap as well as a deadline, because a client can also send headers fast. A
# probe request has a handful; a hundred is far past anything legitimate and far
# below what it takes to exhaust memory.
MAX_HEADER_LINES = 100

# How long `stop()` waits for live handlers before taking their sockets away.
# Shutdown is the caller's deadline, not the client's: a probe endpoint has no
# request worth blocking a SIGTERM for, and a container's grace period ends in
# SIGKILL mid-dispatch if this waits.
SHUTDOWN_GRACE_SECONDS = 1.0


class HealthListener:
    #: Tests set this to 0 so no test ever binds a fixed port on a shared runner.
    _test_port_override: int | None = None

    def __init__(self, service: Any, host: str, port: int | None = None):
        """Bind a probe listener for `service`.

        `port` is consulted ONLY when the service has no `config.health_port`.
        A service that has one -- which is every `CliffracerService` -- binds
        that, resolved in `start()` so a runtime override is honoured, and the
        argument is ignored. It is optional for that reason: passing a port to
        a listener whose service is configured does nothing, and a signature
        that demands one implies otherwise.

        `None`, like `0`, asks the operating system for a free port.
        """
        self.service = service
        self.host = host
        self._requested_port = port
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        # Live handler writers, so `stop()` can abort what will not finish.
        self._connections: set[asyncio.StreamWriter] = set()
        self._disabled: str | None = None

    def disable(self, reason: str) -> None:
        """An extension that serves the same routes on the same port calls this."""
        self._disabled = reason

    @property
    def _log(self) -> Any:
        """The loguru logger bound to the service this listener serves."""
        name = getattr(getattr(self.service, "config", None), "name", None)
        return logger.bind(service=name) if name else logger

    async def start(self) -> None:
        """Bind the probe listener, or raise.

        A port already in use raises OSError. Two services in one process
        therefore need different `health_port` values, and a probe that points
        at 8000 reaches the service that owns 8000 or nothing at all.

        `health_port=0` asks the operating system for a free port, the usual
        socket idiom. The bound port is then readable from `/info` and from
        the line this logs at startup.

        WHICH PORT WINS: `config.health_port` whenever the service has one,
        otherwise the constructor's `port`. The config is read here rather than
        at construction so a runtime override is honoured -- and that is why
        the constructor argument cannot be what decides for a configured
        service. Passing 0 to the constructor of a listener whose service names
        a port does NOT get an ephemeral port; it gets the configured one.
        """
        if self._disabled:
            self._log.info(f"health listener not started: {self._disabled}")
            return
        # Resolve health port at start rather than construction to reflect runtime configuration overrides.
        requested = self._requested_port
        config = getattr(self.service, "config", None)
        if config is not None and hasattr(config, "health_port"):
            requested = config.health_port
        # An unspecified port is passed through as None, which
        # `asyncio.start_server` treats exactly as it treats 0: it asks the
        # operating system for a free one. Normalising it to 0 here changed
        # nothing that any test could observe -- the bound port is read back
        # from `getsockname()` either way -- so the assumption is asserted in
        # `tests/unit/test_which_health_port_decides.py` instead of restated
        # in code that cannot fail.
        port = requested if self._test_port_override is None else self._test_port_override
        try:
            self._server = await asyncio.start_server(self._handle, self.host, port)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                self._log.error(
                    f"health listener failed to bind {self.host}:{port}: port already in use"
                )
            raise
        self.port = self._server.sockets[0].getsockname()[1]
        self._log.info(f"health listener on http://{self.host}:{self.port}/health")

    async def stop(self) -> None:
        """Stop accepting, then stop waiting.

        `Server.wait_closed()` does not return until every live handler has
        finished, so awaiting it unbounded let one client decide when this
        service could shut down. `LifecycleManager` awaits this as step 2 of
        shutdown, before it cancels subscriptions, so the service went on
        consuming messages while it waited.

        Live handlers get `SHUTDOWN_GRACE_SECONDS` to finish -- enough for a
        probe that is mid-response -- and then their sockets are aborted. A
        probe cut off during shutdown reads as a failed probe, which is true.
        """
        if self._server is None:
            return
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            held = len(self._connections)
            self._log.warning(
                f"health listener still had {held} open connection(s) after "
                f"{SHUTDOWN_GRACE_SECONDS}s; aborting them so shutdown proceeds"
            )
            for writer in list(self._connections):
                try:
                    writer.transport.abort()
                except Exception:  # noqa: BLE001 - a socket already gone is fine
                    pass
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=SHUTDOWN_GRACE_SECONDS)
            except TimeoutError:
                self._log.warning("health listener did not close cleanly; continuing shutdown")
        finally:
            self._connections.clear()
            self._server = None
            self.port = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._connections.add(writer)
        path = ""
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + REQUEST_DEADLINE_SECONDS

            def remaining() -> float:
                # A single budget for the whole request. Each read gets what is
                # left of it, so a client cannot extend its handler by sending
                # more.
                return max(0.0, deadline - loop.time())

            request_line = await asyncio.wait_for(reader.readline(), timeout=remaining())
            for _ in range(MAX_HEADER_LINES):
                line = await asyncio.wait_for(reader.readline(), timeout=remaining())
                if line in (b"\r\n", b"\n", b""):
                    break
            else:
                await self._respond(writer, 431, {"error": "too many header lines"})
                return
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
                    if not isinstance(live_data, dict):
                        # An `is_live()` that answers a boolean, as its name reads.
                        live_data = {
                            "service": getattr(
                                getattr(self.service, "config", None), "name", "service"
                            ),
                            "status": "healthy" if live_data else "stopped",
                        }
                else:
                    # A host that does not say whether it is running is not reported
                    # healthy: "could not determine" is not "yes".
                    running = getattr(self.service, "_running", None)
                    service_name = getattr(getattr(self.service, "config", None), "name", "service")
                    live_data = {
                        "service": service_name,
                        "status": "unknown"
                        if running is None
                        else ("healthy" if running else "stopped"),
                    }
                status_code = 200 if live_data.get("status") == "healthy" else 503
                await self._respond(writer, status_code, live_data)
            elif path in ("/ready", "/health"):
                body = await self.service.health_check()
                await self._respond(writer, 200 if body.get("status") == "healthy" else 503, body)
            elif path == "/info":
                info = dict(self.service.get_service_info())
                info["health_port"] = self.port
                await self._respond(writer, 200, info)
            else:
                await self._respond(writer, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 - the probe must get an answer
            self._log.warning(f"health listener request failed: {exc}")
            try:
                # The probe must get an answer; it must not get the exception.
                # `str(exc)` here is whatever health_check, get_service_info or
                # a probe raised, on a port with no authentication in front of
                # it. The log above keeps the detail.
                text = exception_text(exc, getattr(self.service, "config", None))
                if path in ("/live", "/ready", "/health"):
                    # A status probe whose evaluation crashed has not found the service healthy,
                    # and the one status the endpoint documents for that is 503 (ADR-0003); a
                    # prober reading codes, not bodies, must see "unavailable". `/info` is no
                    # probe, so a crash there stays a 500.
                    await self._respond(writer, 503, {"status": "error", "error": text})
                else:
                    await self._respond(writer, 500, {"error": text})
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._connections.discard(writer)
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
            431: "Request Header Fields Too Large",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }[status]
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()

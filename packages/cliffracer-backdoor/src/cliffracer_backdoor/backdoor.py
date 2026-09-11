"""Loopback debug console for running services.

Serves an interactive async Python shell over a TCP loopback socket using
aioconsole and asyncio streams.
"""

import asyncio
import hashlib
import hmac
import os
import time
from typing import Any

from loguru import logger

from cliffracer.core.service import redact_nats_url

# A LITERAL loopback address, never the name "localhost" -- see start().
BACKDOOR_HOST = "127.0.0.1"

try:
    import aioconsole

    HAS_AIOCONSOLE = True
except ImportError:
    HAS_AIOCONSOLE = False
    logger.warning("aioconsole not installed - backdoor will use basic REPL")


class BackdoorServer:
    """Async debug console server bound to loopback.

    Accepts TCP connections on 127.0.0.1, enforces password authentication with
    failed-attempt lockout, and runs an interactive REPL with service and NATS
    inspection helpers exposed in the local namespace.
    """

    def __init__(
        self,
        service_instance: Any,
        port: int = 0,
        enabled: bool = True,
        password: str | None = None,
    ):
        """
        Initialize async backdoor server.

        Args:
            service_instance: The running service to debug
            port: Port to bind to (0 for auto-assign)
            enabled: Whether backdoor is enabled
            password: Authentication password. BackdoorExtension supplies it
                from CLIFFRACER_BACKDOOR_PASSWORD; a random one is generated
                when it is None and the console is enabled.
        """
        self.service = service_instance
        self.port = port
        self.enabled = enabled
        self.server: asyncio.Server | None = None

        # Password is read from configuration model.
        self.password = password
        if not self.password and self.enabled:
            logger.warning("Backdoor enabled without password! Generating random password...")
            self.password = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
            logger.info(f"Backdoor password: {self.password}")

        # Security settings
        self.max_auth_attempts = 3
        self.auth_timeout = 30  # seconds
        self.lockout_duration = 300  # 5 minutes
        self.failed_auth_attempts: dict[str, list[float]] = {}  # IP -> timestamps

        # Active client tracking for clean shutdown
        self._active_clients: set[asyncio.Task] = set()
        self._client_writers: set[asyncio.StreamWriter] = set()

    def _build_context(self) -> dict[str, Any]:
        """Build the context dict available in the backdoor shell."""
        context = {
            "service": self.service,
            "nc": getattr(self.service, "nc", None),
            "js": getattr(self.service, "js", None),
            "asyncio": asyncio,
            "help_backdoor": self._help_backdoor,
            "inspect_service": self._inspect_service,
            "inspect_nats": self._inspect_nats,
            "show_handlers": self._show_handlers,
        }
        return context

    async def start(self) -> int | None:
        """
        Start the async backdoor server.

        Returns:
            Port number if started, None if disabled
        """
        if not self.enabled:
            logger.debug("Backdoor server disabled")
            return None

        if not HAS_AIOCONSOLE:
            logger.error(
                "aioconsole not installed - backdoor disabled. Install with: pip install aioconsole"
            )
            return None

        try:
            # Bind to literal 127.0.0.1 to avoid dual-stack socket allocation with ephemeral ports.
            self.server = await asyncio.start_server(
                self._handle_client,
                host=BACKDOOR_HOST,
                port=self.port,
            )

            # Get actual port if auto-assigned
            addr = self.server.sockets[0].getsockname()
            self.port = addr[1]

            logger.info(f"Backdoor server started on {BACKDOOR_HOST}:{self.port}")
            logger.info(f"   Connect with: cliffracer-backdoor {BACKDOOR_HOST}:{self.port}")
            if self.password:
                logger.info("   Password required for access")

            return self.port

        except Exception as e:
            logger.error(f"Failed to start backdoor server: {e}")
            return None

    async def stop(self) -> None:
        """Stop the async backdoor server."""
        if self.server:
            self.server.close()

        for task in self._active_clients:
            task.cancel()

        for writer in self._client_writers:
            try:
                writer.close()
            except Exception:
                pass

        if self.server:
            await self.server.wait_closed()

        logger.info("Backdoor server stopped")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """
        Handle individual client connection.

        This is fully async - no threading needed!
        """
        addr = writer.get_extra_info("peername")
        client_ip = addr[0] if addr else "unknown"
        connection_id = f"{client_ip}:{addr[1]}" if addr else "unknown"

        logger.info(f"Backdoor connection from {connection_id}")

        current_task = asyncio.current_task()
        if current_task:
            self._active_clients.add(current_task)
        self._client_writers.add(writer)

        try:
            # Check if IP is locked out
            if self._is_locked_out(client_ip):
                writer.write(b"Too many failed attempts. Try again later.\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            # Authenticate user
            if not await self._authenticate(reader, writer, client_ip):
                writer.close()
                await writer.wait_closed()
                return

            # Send welcome message
            welcome = self._get_welcome_message()
            writer.write(welcome.encode())
            await writer.drain()

            # Start interactive session
            await self._run_console(reader, writer)

        except Exception as e:
            logger.error(f"Backdoor connection error from {connection_id}: {e}")
        finally:
            current_task = asyncio.current_task()
            if current_task and current_task in self._active_clients:
                self._active_clients.remove(current_task)
            if writer in self._client_writers:
                self._client_writers.remove(writer)

            try:
                writer.close()
                await writer.wait_closed()
                logger.info(f"Backdoor connection {connection_id} closed")
            except Exception:
                pass

    async def _authenticate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, client_ip: str
    ) -> bool:
        """Authenticate client connection."""
        writer.write(b"\xf0\x9f\x94\x90 Backdoor Authentication Required\n")
        writer.write(b"Password: ")
        await writer.drain()

        try:
            # Read password with timeout
            password_bytes = await asyncio.wait_for(
                reader.readuntil(b"\n"), timeout=self.auth_timeout
            )
            password_input = password_bytes.decode().strip()

            # Verify password
            if self._verify_password(password_input):
                writer.write(b"\n\xe2\x9c\x85 Authentication successful!\n\n")
                await writer.drain()
                self._reset_failed_attempts(client_ip)
                return True
            else:
                self._record_failed_attempt(client_ip)
                writer.write(b"\n\xe2\x9d\x8c Authentication failed!\n")
                await writer.drain()
                return False

        except TimeoutError:
            writer.write(b"\nAuthentication timeout.\n")
            await writer.drain()
            return False
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return False

    async def _run_console(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """
        Run the interactive console.

        Uses aioconsole for async-native REPL.
        """
        try:
            # aioconsole.interact signature:
            # (banner=None, streams=None, locals=None, stop=True, handle_sigint=True)
            session_locals = self._build_context()
            await aioconsole.interact(
                banner="",
                streams=(reader, writer),
                locals=session_locals,
                stop=False,  # Don't stop event loop when console exits
                handle_sigint=False,  # Let service handle signals
            )
        except EOFError:
            # Normal exit via Ctrl+D
            writer.write(b"\nGoodbye!\n")
            await writer.drain()
        except Exception as e:
            logger.error(f"Console error: {e}")
            import traceback

            traceback.print_exc()
            writer.write(f"\nError: {e}\n".encode())
            await writer.drain()

    def _verify_password(self, password: str) -> bool:
        """Verify password."""
        if self.password is None:
            return False
        return hmac.compare_digest(password, self.password)

    def _is_locked_out(self, client_ip: str) -> bool:
        """Check if client IP is locked out due to failed auth attempts."""
        if client_ip not in self.failed_auth_attempts:
            return False

        attempts = self.failed_auth_attempts[client_ip]
        current_time = time.time()

        # Remove old attempts outside lockout window
        attempts[:] = [t for t in attempts if current_time - t < self.lockout_duration]

        # Check if too many recent attempts
        return len(attempts) >= self.max_auth_attempts

    def _record_failed_attempt(self, client_ip: str) -> None:
        """Record a failed authentication attempt."""
        if client_ip not in self.failed_auth_attempts:
            self.failed_auth_attempts[client_ip] = []
        self.failed_auth_attempts[client_ip].append(time.time())

    def _reset_failed_attempts(self, client_ip: str) -> None:
        """Reset failed attempts for an IP after successful auth."""
        if client_ip in self.failed_auth_attempts:
            del self.failed_auth_attempts[client_ip]

    def _get_welcome_message(self) -> str:
        """Get welcome message for backdoor shell."""
        service_name = getattr(self.service, "config", None)
        service_name = service_name.name if service_name else "unknown"

        msg = f"""
╔══════════════════════════════════════════════════════════╗
║  Cliffracer Backdoor - Service Debug Shell              ║
╚══════════════════════════════════════════════════════════╝

Service: {service_name}
Type: {self.service.__class__.__name__}

Available commands:
  service          - Service instance
  nc               - NATS connection
  js               - JetStream context
  help_backdoor()  - Show all available helpers
  inspect_service()- Inspect service state
  inspect_nats()   - Inspect NATS connection
  show_handlers()  - Show registered handlers

Tips:
  - Async code works naturally! Just use 'await'
  - Use Ctrl+D or type 'exit()' to disconnect

"""
        return msg

    def _help_backdoor(self) -> None:
        """Show help for backdoor commands."""
        help_text = """
Backdoor Helper Commands:
========================

inspect_service()  - Show service configuration and state
inspect_nats()     - Show NATS connection info and subscriptions
show_handlers()    - List all registered RPC and event handlers

Variables:
  service  - The running service instance
  nc       - NATS connection object
  js       - JetStream context
  asyncio  - asyncio module

Examples:
  # Call an RPC method directly
  await service.my_rpc_method(param="value")

  # Inspect NATS subscriptions
  inspect_nats()

  # Publish a test event
  await nc.publish("test.event", b'{"test": true}')

  # Check service handlers
  show_handlers()
"""
        print(help_text)

    def _inspect_service(self) -> None:
        """Inspect service state."""
        config = getattr(self.service, "config", None)
        print(f"\n{'=' * 60}")
        print(f"Service Inspection: {self.service.__class__.__name__}")
        print(f"{'=' * 60}")

        if config:
            print("\nConfiguration:")
            print(f"  Name: {config.name}")
            print(f"  NATS URL: {redact_nats_url(config.nats_url)}")
            print(f"  Request Timeout: {config.request_timeout}s")

        print(f"\nRunning: {getattr(self.service, '_running', 'unknown')}")

        if hasattr(self.service, "_rpc_handlers"):
            print(f"\nRPC Handlers: {len(self.service._rpc_handlers)}")
            for name in list(self.service._rpc_handlers.keys())[:10]:
                print(f"  - {name}")
            if len(self.service._rpc_handlers) > 10:
                print(f"  ... and {len(self.service._rpc_handlers) - 10} more")

        if hasattr(self.service, "_event_handlers"):
            print(f"\nEvent Handlers: {len(self.service._event_handlers)}")
            for pattern in list(self.service._event_handlers.keys())[:10]:
                print(f"  - {pattern}")
            if len(self.service._event_handlers) > 10:
                print(f"  ... and {len(self.service._event_handlers) - 10} more")

        print(f"\n{'=' * 60}\n")

    def _inspect_nats(self) -> None:
        """Inspect NATS connection."""
        nc = getattr(self.service, "nc", None)

        print(f"\n{'=' * 60}")
        print("NATS Connection Inspection")
        print(f"{'=' * 60}")

        if not nc:
            print("No NATS connection available")
            return

        print(f"\nConnected: {nc.is_connected}")
        print(f"Closed: {nc.is_closed}")
        print(f"Reconnecting: {nc.is_reconnecting}")
        print(f"Connected URL: {nc.connected_url if hasattr(nc, 'connected_url') else 'N/A'}")

        if hasattr(self.service, "_subscriptions"):
            print(f"\nActive Subscriptions: {len(self.service._subscriptions)}")

        print(f"\n{'=' * 60}\n")

    def _show_handlers(self) -> None:
        """Show all registered handlers."""
        print(f"\n{'=' * 60}")
        print("Registered Handlers")
        print(f"{'=' * 60}")

        if hasattr(self.service, "_rpc_handlers"):
            print(f"\nRPC Handlers ({len(self.service._rpc_handlers)}):")
            for name, handler in self.service._rpc_handlers.items():
                print(f"  {name:30} -> {handler.__name__}")

        if hasattr(self.service, "_event_handlers"):
            print(f"\nEvent Handlers ({len(self.service._event_handlers)}):")
            for pattern, handler in self.service._event_handlers.items():
                print(f"  {pattern:30} -> {handler.__name__}")

        if hasattr(self.service, "_timers"):
            print(f"\nTimers ({len(self.service._timers)}):")
            for timer in self.service._timers:
                print(f"  {timer.method_name:30} (interval: {timer.interval}s)")

        print(f"\n{'=' * 60}\n")


class BackdoorClient:
    """
    Client for connecting to Cliffracer backdoor servers.
    """

    @staticmethod
    def connect(host: str = BACKDOOR_HOST, port: int = 9999) -> None:
        """
        Connect to a backdoor server.

        Args:
            host: Server hostname
            port: Server port
        """
        try:
            import subprocess

            # Try to use netcat first, then telnet
            for cmd in [["nc", host, str(port)], ["telnet", host, str(port)]]:
                try:
                    subprocess.run(cmd, check=True)
                    return
                except (subprocess.CalledProcessError, FileNotFoundError):
                    continue

            # If no tools available, show instructions
            print("Connect manually with:")
            print(f"   nc {host} {port}")
            print(f"   telnet {host} {port}")

        except Exception as e:
            logger.error(f"Failed to connect to backdoor: {e}")

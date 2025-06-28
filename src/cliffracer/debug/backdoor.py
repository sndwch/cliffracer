"""
Cliffracer Backdoor Server

Provides a live Python shell for debugging running services.
Inspired by Nameko's backdoor but designed for NATS-based services.
"""

import code
import logging
import socket
import sys
import threading
from datetime import datetime
from typing import Any

from cliffracer.core.service_config import ServiceConfig

logger = logging.getLogger(__name__)


class BackdoorServer:
    """
    Live debugging server that provides Python shell access to running services.

    Features:
    - Interactive Python shell with service context
    - NATS connection inspection
    - Service state debugging
    - Global enable/disable configuration
    """

    def __init__(self, service_instance, port: int = 0, enabled: bool = True):
        """
        Initialize backdoor server.

        Args:
            service_instance: The running service to debug
            port: Port to bind to (0 for auto-assign)
            enabled: Whether backdoor is enabled
        """
        self.service = service_instance
        self.port = port
        self.enabled = enabled
        self.server: socket.socket | None = None
        self.server_thread: threading.Thread | None = None
        self.running = False
        self.connections: dict[str, Any] = {}

        # Context variables available in backdoor shell
        self.context = {
            "service": service_instance,
            "help_backdoor": self._help_backdoor,
            "inspect_service": self._inspect_service,
            "inspect_nats": self._inspect_nats,
            "list_workers": self._list_workers,
            "show_metrics": self._show_metrics,
        }

    def start(self) -> int | None:
        """
        Start the backdoor server.

        Returns:
            Port number if started, None if disabled
        """
        if not self.enabled:
            logger.debug("Backdoor server disabled")
            return None

        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind(("localhost", self.port))
            self.server.listen(5)

            # Get actual port if auto-assigned
            self.port = self.server.getsockname()[1]

            self.running = True
            self.server_thread = threading.Thread(
                target=self._run_server, daemon=True, name=f"backdoor-server-{self.port}"
            )
            self.server_thread.start()

            logger.info(f"🔧 Backdoor server started on localhost:{self.port}")
            logger.info(f"   Connect with: nc localhost {self.port}")

            return self.port

        except Exception as e:
            logger.error(f"Failed to start backdoor server: {e}")
            return None

    def stop(self):
        """Stop the backdoor server."""
        if not self.running:
            return

        self.running = False

        if self.server:
            try:
                self.server.close()
            except Exception as e:
                logger.error(f"Error closing backdoor server: {e}")

        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=1.0)

        logger.info("🔧 Backdoor server stopped")

    def _run_server(self):
        """Main server loop handling connections."""
        while self.running:
            try:
                if not self.server:
                    break

                conn, addr = self.server.accept()

                # Handle connection in separate thread
                conn_thread = threading.Thread(
                    target=self._handle_connection,
                    args=(conn, addr),
                    daemon=True,
                    name=f"backdoor-conn-{addr[0]}:{addr[1]}",
                )
                conn_thread.start()

            except OSError:
                # Server socket was closed
                break
            except Exception as e:
                if self.running:
                    logger.error(f"Backdoor server error: {e}")
                break

    def _handle_connection(self, conn: socket.socket, addr):
        """Handle individual backdoor connection."""
        connection_id = f"{addr[0]}:{addr[1]}"
        logger.info(f"🔧 Backdoor connection from {connection_id}")

        try:
            # Convert socket to file-like objects
            conn_file = conn.makefile("rw")

            # Send welcome message
            welcome = self._get_welcome_message()
            conn_file.write(welcome)
            conn_file.flush()

            # Create interactive console with service context
            console = code.InteractiveConsole(locals=self.context)

            # Redirect stdout/stderr to connection
            old_stdout = sys.stdout
            old_stderr = sys.stderr

            try:
                sys.stdout = conn_file
                sys.stderr = conn_file

                # Start interactive session
                console.interact(banner="")

            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr

        except Exception as e:
            logger.error(f"Backdoor connection error: {e}")
        finally:
            try:
                conn.close()
                logger.info(f"🔧 Backdoor connection {connection_id} closed")
            except Exception:
                pass

    def _get_welcome_message(self) -> str:
        """Generate welcome message for backdoor session."""
        return f"""
🚀 Cliffracer Backdoor - Live Service Debugging
{"=" * 50}
Service: {getattr(self.service, "name", "Unknown")}
Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

Available commands:
  help_backdoor()    - Show available debugging commands
  inspect_service()  - Show service information
  inspect_nats()     - Show NATS connection details
  list_workers()     - Show active workers
  show_metrics()     - Show service metrics

Variables:
  service           - Your service instance

Type help() for Python help, exit() to disconnect.
{"=" * 50}

"""

    def _help_backdoor(self):
        """Show backdoor-specific help."""
        help_text = """
🔧 Cliffracer Backdoor Commands:

Service Inspection:
  inspect_service()     - Show service configuration and state
  inspect_nats()        - Show NATS connection information
  list_workers()        - Show currently running workers
  show_metrics()        - Show performance metrics

Direct Access:
  service              - Your service instance
  service.nats         - NATS connection (if available)
  service.config       - Service configuration

Examples:
  # Check if NATS is connected
  service.nats.is_connected if hasattr(service, 'nats') else 'No NATS'

  # Get service configuration
  vars(service.config)

  # Manual RPC call (if service supports it)
  await service.some_rpc_method(arg1, arg2)

  # Check service state
  inspect_service()
"""
        print(help_text)

    def _inspect_service(self):
        """Inspect current service state."""
        info = {
            "service_name": getattr(self.service, "name", "Unknown"),
            "service_class": self.service.__class__.__name__,
            "config": getattr(self.service, "config", None),
            "nats_connected": self._check_nats_connection(),
            "methods": [m for m in dir(self.service) if not m.startswith("_")],
        }

        print("🔍 Service Information:")
        for key, value in info.items():
            if key == "config" and value:
                print(f"  {key}: {type(value).__name__}")
                for attr in ["name", "nats_url", "auto_restart"]:
                    if hasattr(value, attr):
                        print(f"    {attr}: {getattr(value, attr)}")
            elif key == "methods":
                print(
                    f"  {key}: {', '.join(value[:10])}"
                    + (f" ... ({len(value)} total)" if len(value) > 10 else "")
                )
            else:
                print(f"  {key}: {value}")

    def _inspect_nats(self):
        """Inspect NATS connection details."""
        if not hasattr(self.service, "nats") or not self.service.nats:
            print("❌ No NATS connection available")
            return

        nats = self.service.nats

        info = {
            "connected": getattr(nats, "is_connected", False),
            "servers": getattr(nats, "servers", []),
            "client_id": getattr(nats, "_client_id", None),
            "pending_data_size": getattr(nats, "_pending_data_size", 0),
        }

        print("📡 NATS Connection Information:")
        for key, value in info.items():
            print(f"  {key}: {value}")

        # Show subscriptions if available
        if hasattr(nats, "_subs"):
            subs = getattr(nats, "_subs", {})
            print(f"  subscriptions: {len(subs)} active")
            for sid, sub in list(subs.items())[:5]:  # Show first 5
                print(f"    {sid}: {getattr(sub, 'subject', 'unknown')}")
            if len(subs) > 5:
                print(f"    ... and {len(subs) - 5} more")

    def _list_workers(self):
        """List currently active workers."""
        # This would need to be implemented based on how Cliffracer tracks workers
        # For now, show a placeholder
        print("👷 Active Workers:")
        print("  (Worker tracking not yet implemented)")
        print("  Use: vars(service) to inspect service state")

    def _show_metrics(self):
        """Show service metrics."""
        print("📊 Service Metrics:")
        print("  (Metrics collection not yet implemented)")
        print("  Service uptime: Available via service inspection")

    def _check_nats_connection(self) -> bool:
        """Check if NATS is connected."""
        if hasattr(self.service, "nats") and self.service.nats:
            return getattr(self.service.nats, "is_connected", False)
        return False


class BackdoorClient:
    """
    Client for connecting to Cliffracer backdoor servers.
    """

    @staticmethod
    def connect(host: str = "localhost", port: int = 9999):
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
            print("💡 Connect manually with:")
            print(f"   nc {host} {port}")
            print(f"   telnet {host} {port}")

        except Exception as e:
            logger.error(f"Failed to connect to backdoor: {e}")


def is_backdoor_enabled(config: ServiceConfig | None = None) -> bool:
    """
    Check if backdoor is globally enabled.

    Args:
        config: Service configuration

    Returns:
        True if backdoor should be enabled
    """
    import os

    # Check environment variable first
    env_disabled = os.getenv("CLIFFRACER_DISABLE_BACKDOOR", "").lower()
    if env_disabled in ("1", "true", "yes"):
        return False

    # Check config if provided
    if config and hasattr(config, "disable_backdoor"):
        return not config.disable_backdoor

    # Default: enabled in development, check environment
    return os.getenv("CLIFFRACER_ENV", "development") == "development"

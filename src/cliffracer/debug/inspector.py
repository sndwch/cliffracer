"""
Service and NATS inspection utilities for debugging.
"""

import asyncio
import inspect
import sys
from datetime import datetime
from typing import Any


class ServiceInspector:
    """
    Utility for inspecting Cliffracer service state and configuration.
    """

    def __init__(self, service):
        self.service = service

    def get_info(self) -> dict[str, Any]:
        """Get comprehensive service information."""
        return {
            "basic": self._get_basic_info(),
            "config": self._get_config_info(),
            "methods": self._get_methods_info(),
            "nats": self._get_nats_info(),
            "runtime": self._get_runtime_info(),
        }

    def _get_basic_info(self) -> dict[str, Any]:
        """Get basic service information."""
        return {
            "name": getattr(self.service, "name", "Unknown"),
            "class": self.service.__class__.__name__,
            "module": self.service.__class__.__module__,
            "id": id(self.service),
        }

    def _get_config_info(self) -> dict[str, Any]:
        """Get service configuration details."""
        config = getattr(self.service, "config", None)
        if not config:
            return {"status": "No configuration found"}

        config_dict = {}
        for attr in dir(config):
            if not attr.startswith("_"):
                try:
                    value = getattr(config, attr)
                    if not callable(value):
                        config_dict[attr] = value
                except Exception:
                    config_dict[attr] = "<error accessing>"

        return config_dict

    def _get_methods_info(self) -> dict[str, Any]:
        """Get information about service methods."""
        methods = {}

        for name in dir(self.service):
            if name.startswith("_"):
                continue

            attr = getattr(self.service, name)
            if callable(attr):
                try:
                    sig = inspect.signature(attr)
                    methods[name] = {
                        "signature": str(sig),
                        "is_coroutine": asyncio.iscoroutinefunction(attr),
                        "doc": inspect.getdoc(attr) or "No documentation",
                    }
                except Exception:
                    methods[name] = {"error": "Cannot inspect"}

        return methods

    def _get_nats_info(self) -> dict[str, Any]:
        """Get NATS connection information."""
        if not hasattr(self.service, "nats"):
            return {"status": "No NATS connection"}

        nats_conn = self.service.nats
        if not nats_conn:
            return {"status": "NATS connection is None"}

        return {
            "connected": getattr(nats_conn, "is_connected", False),
            "servers": getattr(nats_conn, "servers", []),
            "client_id": getattr(nats_conn, "_client_id", None),
            "stats": self._get_nats_stats(nats_conn),
        }

    def _get_nats_stats(self, nats_conn) -> dict[str, Any]:
        """Get NATS connection statistics."""
        try:
            stats = getattr(nats_conn, "stats", {})
            if callable(stats):
                stats = stats()
            return stats
        except Exception:
            return {"error": "Cannot get stats"}

    def _get_runtime_info(self) -> dict[str, Any]:
        """Get runtime information."""
        return {
            "python_version": sys.version,
            "current_time": datetime.now().isoformat(),
            "memory_usage": self._get_memory_usage(),
        }

    def _get_memory_usage(self) -> dict[str, int] | None:
        """Get memory usage information."""
        try:
            import psutil

            process = psutil.Process()
            memory_info = process.memory_info()
            return {
                "rss": memory_info.rss,  # Resident Set Size
                "vms": memory_info.vms,  # Virtual Memory Size
            }
        except ImportError:
            return None

    def print_summary(self):
        """Print a formatted summary of service information."""
        info = self.get_info()

        print("🔍 Service Summary")
        print("=" * 50)

        # Basic info
        basic = info["basic"]
        print(f"Name: {basic['name']}")
        print(f"Class: {basic['class']}")
        print(f"Module: {basic['module']}")

        # NATS status
        nats = info["nats"]
        if "connected" in nats:
            status = "✅ Connected" if nats["connected"] else "❌ Disconnected"
            print(f"NATS: {status}")
        else:
            print(f"NATS: {nats.get('status', 'Unknown')}")

        # Methods count
        methods = info["methods"]
        rpc_methods = [name for name, details in methods.items() if "signature" in details]
        print(f"Methods: {len(rpc_methods)} callable")

        # Runtime
        runtime = info["runtime"]
        if runtime.get("memory_usage"):
            memory_mb = runtime["memory_usage"]["rss"] / 1024 / 1024
            print(f"Memory: {memory_mb:.1f} MB")


class NATSInspector:
    """
    Utility for inspecting NATS connections and state.
    """

    def __init__(self, nats_connection):
        self.nats = nats_connection

    def get_connection_info(self) -> dict[str, Any]:
        """Get detailed NATS connection information."""
        if not self.nats:
            return {"error": "No NATS connection provided"}

        return {
            "status": self._get_connection_status(),
            "servers": self._get_server_info(),
            "subscriptions": self._get_subscription_info(),
            "statistics": self._get_statistics(),
        }

    def _get_connection_status(self) -> dict[str, Any]:
        """Get connection status information."""
        return {
            "connected": getattr(self.nats, "is_connected", False),
            "connecting": getattr(self.nats, "is_connecting", False),
            "closed": getattr(self.nats, "is_closed", False),
            "reconnecting": getattr(self.nats, "is_reconnecting", False),
        }

    def _get_server_info(self) -> dict[str, Any]:
        """Get server information."""
        servers = getattr(self.nats, "servers", [])
        current_server = getattr(self.nats, "_current_server", None)

        return {
            "available_servers": len(servers),
            "current_server": str(current_server) if current_server else None,
            "servers": [str(server) for server in servers[:5]],  # First 5
        }

    def _get_subscription_info(self) -> dict[str, Any]:
        """Get subscription information."""
        subs = getattr(self.nats, "_subs", {})

        subscription_details = []
        for sid, sub in list(subs.items())[:10]:  # First 10 subs
            subscription_details.append(
                {
                    "sid": sid,
                    "subject": getattr(sub, "subject", "unknown"),
                    "queue": getattr(sub, "queue", None),
                    "pending_msgs": getattr(sub, "pending_msgs", 0),
                    "max_pending": getattr(sub, "pending_msgs_limit", None),
                }
            )

        return {
            "total_subscriptions": len(subs),
            "details": subscription_details,
        }

    def _get_statistics(self) -> dict[str, Any]:
        """Get connection statistics."""
        try:
            stats = getattr(self.nats, "stats", None)
            if stats and callable(stats):
                return stats()
            elif stats:
                return dict(stats)
            else:
                return {"error": "No stats available"}
        except Exception as e:
            return {"error": f"Failed to get stats: {e}"}

    def print_summary(self):
        """Print a formatted summary of NATS information."""
        info = self.get_connection_info()

        print("📡 NATS Connection Summary")
        print("=" * 50)

        # Connection status
        status = info["status"]
        if status["connected"]:
            print("Status: ✅ Connected")
        elif status["connecting"]:
            print("Status: 🔄 Connecting")
        elif status["reconnecting"]:
            print("Status: 🔄 Reconnecting")
        else:
            print("Status: ❌ Disconnected")

        # Server info
        servers = info["servers"]
        print(f"Servers: {servers['available_servers']} available")
        if servers["current_server"]:
            print(f"Current: {servers['current_server']}")

        # Subscriptions
        subs = info["subscriptions"]
        print(f"Subscriptions: {subs['total_subscriptions']} active")

        # Statistics
        stats = info["statistics"]
        if "error" not in stats:
            for key, value in stats.items():
                if key in ["in_msgs", "out_msgs", "in_bytes", "out_bytes"]:
                    print(f"{key}: {value}")

    def list_subscriptions(self, limit: int = 20):
        """List active subscriptions with details."""
        subs = getattr(self.nats, "_subs", {})

        print(f"📋 Active Subscriptions ({len(subs)} total)")
        print("=" * 50)

        for i, (sid, sub) in enumerate(subs.items()):
            if i >= limit:
                print(f"... and {len(subs) - limit} more")
                break

            subject = getattr(sub, "subject", "unknown")
            queue = getattr(sub, "queue", None)
            pending = getattr(sub, "pending_msgs", 0)

            queue_str = f" (queue: {queue})" if queue else ""
            pending_str = f" [{pending} pending]" if pending > 0 else ""

            print(f"{i + 1:2}. {subject}{queue_str}{pending_str}")


def create_debug_context(service) -> dict[str, Any]:
    """
    Create a debugging context with useful variables and functions.

    Args:
        service: The service instance to debug

    Returns:
        Dictionary of variables and functions for debugging
    """
    context = {
        # Core objects
        "service": service,
        # Inspectors
        "service_inspector": ServiceInspector(service),
        "nats_inspector": NATSInspector(getattr(service, "nats", None))
        if hasattr(service, "nats")
        else None,
        # Utility functions
        "inspect_service": lambda: ServiceInspector(service).print_summary(),
        "inspect_nats": lambda: NATSInspector(getattr(service, "nats", None)).print_summary()
        if hasattr(service, "nats")
        else print("No NATS connection"),
        # Standard debugging tools
        "pprint": __import__("pprint").pprint,
        "datetime": datetime,
        "asyncio": asyncio,
    }

    # Add NATS-specific shortcuts if available
    if hasattr(service, "nats") and service.nats:
        context["nats"] = service.nats
        context["list_subs"] = lambda: NATSInspector(service.nats).list_subscriptions()

    return context

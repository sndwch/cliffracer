"""
CLI command for connecting to Cliffracer backdoor servers.
"""

import argparse
import sys

from cliffracer.debug import BackdoorClient


def create_backdoor_parser(subparsers):
    """Create backdoor CLI parser."""
    parser = subparsers.add_parser(
        "backdoor", help="Connect to a running service backdoor for debugging"
    )

    parser.add_argument(
        "connection", help="Connection string in format host:port (e.g., localhost:9999)"
    )

    parser.set_defaults(func=backdoor_command)


def backdoor_command(args):
    """Handle backdoor CLI command."""
    try:
        if ":" in args.connection:
            host, port_str = args.connection.split(":", 1)
            port = int(port_str)
        else:
            print("❌ Invalid connection format. Use host:port (e.g., localhost:9999)")
            return 1

        print(f"🔧 Connecting to Cliffracer backdoor at {host}:{port}")
        print("💡 Use Ctrl+C to disconnect")
        print()

        BackdoorClient.connect(host, port)
        return 0

    except KeyboardInterrupt:
        print("\\n👋 Disconnected from backdoor")
        return 0
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        return 1


def main():
    """Main entry point for backdoor CLI."""
    parser = argparse.ArgumentParser(description="Connect to Cliffracer service backdoor")

    parser.add_argument(
        "connection", help="Connection string in format host:port (e.g., localhost:9999)"
    )

    args = parser.parse_args()
    sys.exit(backdoor_command(args))


if __name__ == "__main__":
    main()

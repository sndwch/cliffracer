"""`cliffracer-backdoor host:port` -- connect to a running service's console."""

from __future__ import annotations

import argparse
import socket
import sys

from cliffracer_backdoor.backdoor import BackdoorClient

CONNECT_TIMEOUT_S = 5.0


def parse_endpoint(connection: str) -> tuple[str, int]:
    """ "host:port" -> ("host", port). Raises ValueError with a usable message."""
    host, sep, port = connection.rpartition(":")
    if not sep or not host:
        raise ValueError(f"expected host:port, got {connection!r}")
    try:
        return host, int(port)
    except ValueError:
        raise ValueError(f"port must be a number, got {port!r}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cliffracer-backdoor",
        description="Connect to a running cliffracer service's debug console.",
    )
    parser.add_argument("connection", help="host:port, e.g. 127.0.0.1:9999")
    args = parser.parse_args(argv)

    try:
        host, port = parse_endpoint(args.connection)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # PROBE FIRST, because BackdoorClient.connect cannot report failure.
    # It shells out to `nc` then `telnet`, and when both fail it PRINTS
    # instructions and returns normally -- so delegating straight to it makes
    # "nothing is listening" indistinguishable from a session that ended. One
    # socket connect gives this command a real exit status.
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S):
            pass
    except OSError as exc:
        print(f"error: could not connect to {host}:{port}: {exc}", file=sys.stderr)
        return 1

    print(f"connecting to {host}:{port} (Ctrl-C to disconnect)")
    try:
        BackdoorClient.connect(host, port)
    except KeyboardInterrupt:
        print("disconnected")
    return 0


if __name__ == "__main__":
    sys.exit(main())

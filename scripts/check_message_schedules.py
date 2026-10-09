"""Run the message-schedule contract on a disposable nats-server 2.12, allowing no skips.

Scheduled publishing (`service.schedules`) needs nats-server 2.12 or later, newer than the broker
the suite runs against, so its live rows are deselected everywhere but here. This starts a
pinned 2.12 container on a loopback port, points the suite at it, collects the rows, and fails
unless every one of them ran and passed. `--broker-url` uses an existing broker of 2.12 or later
without managing it.

The port is one this script finds free, by binding port 0 on 127.0.0.1, and maps fixed: a port
Docker assigns (`-p 127.0.0.1::4222`) changes on every restart of the container, and the contract
restarts it. Its rows that do are told the container's name in `$CLIFFRACER_TEST_SCHEDULE_BROKER`;
with `--broker-url` there is no container of this script's to restart, so those rows are not
collected, and the script says so.
"""

import argparse
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

import nats
import nats.errors

ROOT = Path(__file__).resolve().parents[1]
BROKER_IMAGE = "nats:2.12.0-alpine"
FLOOR = (2, 12)
CONTRACT = "tests/integration/test_a_scheduled_message_is_written_by_the_broker_live.py"
#: The rows that restart the broker, by name: they need a container this script created.
RESTART_ROWS = "broker_restart or broker_is_down"
BROKER_ENV = "CLIFFRACER_TEST_SCHEDULE_BROKER"
#: How many free ports are tried before giving up: another process can take one between finding
#: it free and the container binding it.
PORT_ATTEMPTS = 3


def free_loopback_port():
    """A port free on 127.0.0.1 now, found by binding port 0."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def disposable_broker():
    """A container on a free loopback port; delete only it, even when the contract fails."""
    base = os.environ.get("SCHEDULE_NATS_NAME") or f"cliffracer-schedule-check-{uuid.uuid4().hex}"
    taken = []
    for attempt in range(1, PORT_ATTEMPTS + 1):
        port = free_loopback_port()
        name = base if attempt == 1 else f"{base}-{attempt}"
        created = subprocess.run(
            [
                "docker",
                "create",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{port}:4222",
                BROKER_IMAGE,
                "-js",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )
        container = created.stdout.strip()
        started = subprocess.run(
            ["docker", "start", container], capture_output=True, text=True, timeout=30
        )
        if started.returncode == 0:
            break
        subprocess.run(["docker", "rm", "-f", container], check=True, timeout=30)
        if "allocated" not in started.stderr and "in use" not in started.stderr:
            raise RuntimeError(f"the broker container did not start: {started.stderr.strip()}")
        taken.append(port)
    else:
        raise RuntimeError(
            f"no free loopback port held long enough for the broker to bind it: {taken} were "
            f"each taken between finding it free and starting the container"
        )
    try:
        yield f"nats://127.0.0.1:{port}", container
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=True, timeout=30)


async def check_broker(url):
    """Require a connected broker new enough to run message schedules.

    A port mapped fixed accepts a connection as soon as the container starts, before the server
    in it answers, so a connection the server has not answered yet is tried again until it does.
    """
    nc = nats.NATS()
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    await nc.connect(
                        url,
                        connect_timeout=0.5,
                        allow_reconnect=False,
                        max_reconnect_attempts=20,
                        reconnect_time_wait=0.1,
                    )
                    break
                except (OSError, nats.errors.Error):
                    nc = nats.NATS()
                    await asyncio.sleep(0.2)
        version = nc.connected_server_version
        if (version.major, version.minor) < FLOOR:
            raise RuntimeError(
                f"the broker at {url} is nats-server {version}; message schedules need "
                f"{FLOOR[0]}.{FLOOR[1]} or later"
            )
        print(f"Message-schedule broker: {version} at {url}", flush=True)
    finally:
        await nc.close()


def check_report(path):
    """Use actual test outcomes, including setup/teardown failures and skips."""
    cases = ET.parse(path).findall(".//testcase")
    skipped = sum(case.find("skipped") is not None for case in cases)
    failures = sum(
        case.find("failure") is not None or case.find("error") is not None for case in cases
    )
    passed = len(cases) - skipped - failures
    print(f"Message schedules: {passed} passed, {failures} failed, {skipped} skipped", flush=True)
    if not cases or skipped or failures:
        raise RuntimeError(
            "the message-schedule contract requires executed tests with zero failures and zero "
            "skips"
        )


def run_contract(url, container=None):
    asyncio.run(check_broker(url))
    selection = []
    restart_env = {}
    if container is None:
        selection = ["-k", f"not ({RESTART_ROWS})"]
        print(
            "Message schedules: the broker-restart rows are not collected: they restart the "
            "broker, and --broker-url names one this script does not manage",
            flush=True,
        )
    else:
        restart_env = {BROKER_ENV: container}
    with tempfile.TemporaryDirectory(prefix="cliffracer-schedule-report-") as directory:
        report = Path(directory) / "results.xml"
        env = {
            **os.environ,
            "CLIFFRACER_TEST_NATS_URL": url,
            "CLIFFRACER_TEST_MESSAGE_SCHEDULES": "1",
            **restart_env,
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                CONTRACT,
                *selection,
                "-p",
                "no:cacheprovider",
                "--tb=short",
                f"--junitxml={report}",
            ],
            cwd=ROOT,
            env=env,
            timeout=180,
        )
        if result.returncode:
            raise RuntimeError(f"message-schedule pytest exited {result.returncode}")
        check_report(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--broker-url", help="Use an existing broker without managing its lifecycle"
    )
    args = parser.parse_args()
    if args.broker_url:
        run_contract(args.broker_url)
    else:
        with disposable_broker() as (url, container):
            run_contract(url, container)


if __name__ == "__main__":
    main()

"""Run the KV expiry contract on a disposable supported broker, allowing no skips."""

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

import nats
from cliffracer_kv.markers import require_expiry_markers

ROOT = Path(__file__).resolve().parents[1]
BROKER_IMAGE = "nats:2.11.2-alpine"


@contextmanager
def disposable_broker():
    """Delete only the container created here, even when the contract fails."""
    name = os.environ.get("KV_NATS_NAME") or f"cliffracer-kv-check-{uuid.uuid4().hex}"
    created = subprocess.run(
        ["docker", "create", "--name", name, "-p", "127.0.0.1::4222", BROKER_IMAGE, "-js"],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    container = created.stdout.strip()
    try:
        subprocess.run(["docker", "start", container], check=True, timeout=30)
        mapping = subprocess.run(
            ["docker", "port", container, "4222/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        host, port = mapping.rsplit(":", 1)
        if host != "127.0.0.1" or not port.isdigit():
            raise RuntimeError(f"Unexpected broker port mapping: {mapping!r}")
        yield f"nats://{host}:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=True, timeout=30)


async def check_broker(url):
    """Require a connected, released broker supporting both kinds of expiry."""
    nc = nats.NATS()
    try:
        async with asyncio.timeout(10):
            await nc.connect(
                url,
                connect_timeout=0.5,
                allow_reconnect=False,
                max_reconnect_attempts=20,
                reconnect_time_wait=0.1,
            )
        require_expiry_markers(nc.jetstream())
        print(f"KV compatibility broker: {nc.connected_server_version} at {url}", flush=True)
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
    print(f"KV compatibility: {passed} passed, {failures} failed, {skipped} skipped", flush=True)
    if not cases or skipped or failures:
        raise RuntimeError(
            "KV compatibility requires executed tests with zero failures and zero skips"
        )


def run_contract(url):
    asyncio.run(check_broker(url))
    with tempfile.TemporaryDirectory(prefix="cliffracer-kv-report-") as directory:
        report = Path(directory) / "results.xml"
        env = {**os.environ, "CLIFFRACER_TEST_NATS_URL": url}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/integration/test_kv_expiry_markers_live.py",
                "tests/integration/test_kv_bucket_options_live.py",
                "-p",
                "no:cacheprovider",
                "--tb=short",
                f"--junitxml={report}",
            ],
            cwd=ROOT,
            env=env,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(f"KV compatibility pytest exited {result.returncode}")
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
        with disposable_broker() as url:
            run_contract(url)


if __name__ == "__main__":
    main()

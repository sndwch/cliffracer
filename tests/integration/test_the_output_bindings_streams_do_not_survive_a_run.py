"""The JetStream output test can run twice against one broker and leaves nothing behind.

`test_output_bindings` builds its host with `subject_prefix="east"` written into the config, so the
streams it declares are named `east_SHIPMENT_*` and carry no session prefix, which the suite's
teardown sweeps by. Nothing else removed them either, so a second run read the first run's
messages (`assert 8 == 0` on the ledger stream) and the broker kept the names for good.

This runs that test twice in separate pytest processes against the broker this session uses, and
then lists the broker's streams.
"""

import os
import subprocess
import sys

import nats
import pytest
from nats.js.errors import NotFoundError

from conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

TARGET = (
    "tests/integration/test_output_bindings.py::"
    "test_jetstream_outputs_need_declared_coverage_and_distinguish_generations"
)
LEFTOVERS = ("east_SHIPMENT_PROGRESS", "east_SHIPMENT_FAILURES", "east_SHIPMENT_FOREIGN_LEDGER")


async def _delete_leftovers(js) -> None:
    for name in LEFTOVERS:
        try:
            await js.delete_stream(name)
        except NotFoundError:
            pass


async def _existing(js) -> list[str]:
    found = []
    for name in LEFTOVERS:
        try:
            await js.stream_info(name)
            found.append(name)
        except NotFoundError:
            pass
    return found


def _run_target() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", TARGET, "-p", "no:cacheprovider", "-q", "-x"],
        capture_output=True,
        text=True,
        env={**os.environ, "CLIFFRACER_TEST_NATS_URL": broker_url()},
        timeout=240,
        check=False,
    )


async def test_the_jetstream_output_test_passes_twice_and_leaves_no_stream_behind():
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    try:
        await _delete_leftovers(js)
        first = _run_target()
        second = _run_target()
        left = await _existing(js)
    finally:
        await _delete_leftovers(js)
        await nc.close()

    assert first.returncode == 0, first.stdout[-1500:]
    assert second.returncode == 0, (
        "the second run read the first run's streams:\n" + (second.stdout[-1500:])
    )
    assert left == [], f"the broker still holds {left}"

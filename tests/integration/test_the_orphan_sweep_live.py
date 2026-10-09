"""The hand-run sweep against a real broker: it finds streams past the first page, and deletes only its own.

The sweep reads the broker's stream list. A list read through `streams_info()` is one page of the
server's answer (256 entries), so on a broker holding more the sweep silently ignores the rest;
`all_streams` keeps reading until the server's own `total` is reached. The 300 streams here are more
than one page, and the premise is checked in the test: the single-page read is shorter than the
broker's list.

Every name is under a prefix made up for the test, and the sweep is given a pattern that matches
only that prefix, so it cannot touch another test's streams on a shared broker.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from pathlib import Path

import nats
import pytest
from nats.js.api import KeyValueConfig, StreamConfig

from cliffracer.core.jetstream import all_streams
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sweep_orphan_test_prefixes.py"


def _load():
    spec = importlib.util.spec_from_file_location("_orphan_sweep_live", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sweep_script = _load()

# The sweep is asked about "streams older than -1 hours", i.e. created before an hour from now, so
# every stream that exists counts as old without waiting for one to age and without trusting that the
# broker's clock and this one agree to the second.
EVERYTHING_EXISTING = -1.0


def _prefix() -> str:
    """A name with the shape a test session's prefix has, that no other test will use."""
    return f"t{uuid.uuid4().hex[:6]}m"


async def _names(js, prefix: str) -> list[str]:
    return sorted(
        info.config.name
        for info in await all_streams(js)
        if info.config.name.startswith((f"{prefix}_", f"KV_{prefix}_"))
    )


async def test_a_sweep_reports_by_default_and_with_apply_deletes_streams_and_buckets_not_named_ones():
    prefix = _prefix()
    only_mine = re.compile(rf"^(KV_)?{prefix}_")
    named = f"SWEEPLIVE_NAMED_{uuid.uuid4().hex[:8]}"
    nc = await nats.connect(broker_url(), name="orphan-sweep-live")
    js = nc.jetstream()
    try:
        await js.add_stream(StreamConfig(name=f"{prefix}_ORDERS", subjects=[f"sw.{prefix}.o"]))
        await js.add_stream(StreamConfig(name=f"{prefix}_DLQ", subjects=[f"sw.{prefix}.d"]))
        await js.create_key_value(KeyValueConfig(bucket=f"{prefix}_bucket"))
        await js.add_stream(StreamConfig(name=named, subjects=[f"sw.{prefix}.named"]))
        assert sweep_script.TEST_PREFIX.match(f"{prefix}_ORDERS"), (
            "the names are not the real shape"
        )
        before = await _names(js, prefix)
        assert len(before) == 3

        dry = await sweep_script.sweep(broker_url(), EVERYTHING_EXISTING, False, only_mine)

        assert sorted(name for name, _ in dry.stale) == before
        assert await _names(js, prefix) == before, "a dry run deleted something"

        done = await sweep_script.sweep(broker_url(), EVERYTHING_EXISTING, True, only_mine)

        assert sorted(name for name, _ in done.stale) == before
        assert await _names(js, prefix) == []
        assert (await js.stream_info(named)).config.name == named, "a named stream was deleted"
    finally:
        for name in [*await _names(js, prefix), named]:
            try:
                await js.delete_stream(name)
            except Exception:
                pass
        await nc.close()


async def test_a_sweep_reads_past_the_first_page_of_the_stream_list():
    prefix = _prefix()
    only_mine = re.compile(rf"^(KV_)?{prefix}_")
    count = 300
    nc = await nats.connect(broker_url(), name="orphan-sweep-live-pages")
    js = nc.jetstream()
    try:
        for index in range(count):
            await js.add_stream(
                StreamConfig(name=f"{prefix}_S{index}", subjects=[f"sw.{prefix}.{index}"])
            )
        one_page = await js.streams_info()
        assert len(one_page) < len(await all_streams(js)), "the broker's list fits one page"

        done = await sweep_script.sweep(broker_url(), EVERYTHING_EXISTING, True, only_mine)

        assert len(done.stale) == count
        assert await _names(js, prefix) == []
    finally:
        for name in await _names(js, prefix):
            try:
                await js.delete_stream(name)
            except Exception:
                pass
        await nc.close()

"""The hand-run sweep of leftover test streams deletes a stream only when it is both ours and old.

Leftovers from killed live runs accumulate on a shared broker, and the script that removes them
decides by NAME and AGE. Both decisions are destructive when wrong, so the rules are pinned here:
a test-prefix name older than the threshold goes (a KV bucket included, it is a stream named
`KV_...`); a name that is not a test prefix stays whatever its age; a stream that is too young stays;
a stream whose age the server did not give stays, rather than being read as old; nothing at all is
deleted without `--apply`, and `--apply` refuses an address nobody named.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sweep_orphan_test_prefixes.py"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _load():
    spec = importlib.util.spec_from_file_location("_orphan_sweep_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass resolves its own module by name
    spec.loader.exec_module(module)
    return module


sweep_script = _load()


def stream(name: str, age_hours: float | None) -> SimpleNamespace:
    created = None if age_hours is None else NOW - timedelta(hours=age_hours)
    return SimpleNamespace(config=SimpleNamespace(name=name), created=created)


def names(found: list) -> list[str]:
    return sorted(item[0] if isinstance(item, tuple) else item for item in found)


def test_an_old_test_prefix_stream_is_stale():
    found = sweep_script.plan([stream("t74b41dm_m7b4b_IDEMP_LIVE_STREAM", 30)], NOW, 6)

    assert names(found.stale) == ["t74b41dm_m7b4b_IDEMP_LIVE_STREAM"]


def test_an_old_kv_bucket_under_a_test_prefix_is_stale():
    found = sweep_script.plan([stream("KV_t74b41dm_live_cron_test_bucket", 30)], NOW, 6)

    assert names(found.stale) == ["KV_t74b41dm_live_cron_test_bucket"]


@pytest.mark.parametrize("name", ["JORBO_USER", "east_SHIPMENT_EVENTS", "EXTRACTION", "UTILS_DLQ"])
def test_a_named_service_stream_is_never_stale_however_old(name):
    found = sweep_script.plan([stream(name, 24 * 400)], NOW, 6)

    assert found.stale == [] and found.kept == [name]


@pytest.mark.parametrize(
    "name",
    [
        "tabcd_ORDERS",  # hex but no worker token
        "t74b41dx_ORDERS",  # a worker token that is not m or gw<N>
        "t74b41dm",  # the token alone, no separator
        "xt74b41dm_ORDERS",  # not at the start
        "KV_ORDERS",  # a real bucket
        "KVt74b41dm_ORDERS",  # no underscore after KV
    ],
)
def test_a_name_that_only_looks_like_a_prefix_is_kept(name):
    found = sweep_script.plan([stream(name, 24 * 400)], NOW, 6)

    assert found.stale == [], name


def test_a_test_prefix_stream_younger_than_the_threshold_is_kept():
    found = sweep_script.plan([stream("t74b41dm_ORDERS", 2)], NOW, 6)

    assert found.stale == [] and found.young == ["t74b41dm_ORDERS"]


def test_a_stream_whose_age_the_server_did_not_give_is_kept_not_read_as_old():
    found = sweep_script.plan([stream("t74b41dm_ORDERS", None)], NOW, 6)

    assert found.stale == [] and found.unknown_age == ["t74b41dm_ORDERS"]


def test_the_threshold_is_in_hours_and_exact_at_the_boundary():
    older = sweep_script.plan([stream("t74b41dm_A", 6.01)], NOW, 6)
    younger = sweep_script.plan([stream("t74b41dm_A", 5.99)], NOW, 6)

    assert names(older.stale) == ["t74b41dm_A"]
    assert younger.stale == []


def test_a_pattern_narrows_what_is_deleted():
    mine = re.compile(r"^(KV_)?t74b41dm_mine_")
    found = sweep_script.plan(
        [stream("t74b41dm_mine_A", 30), stream("t74b41dm_theirs_A", 30)], NOW, 6, mine
    )

    assert names(found.stale) == ["t74b41dm_mine_A"]
    assert found.kept == ["t74b41dm_theirs_A"]


def test_the_report_lists_what_would_be_deleted_with_its_age_and_what_is_left_alone():
    found = sweep_script.plan(
        [stream("t74b41dm_OLD", 30), stream("JORBO_USER", 500), stream("t74b41dm_NEW", 1)],
        NOW,
        6,
    )

    dry = "\n".join(sweep_script.render(found, NOW, apply=False))
    applied = "\n".join(sweep_script.render(found, NOW, apply=True))

    assert "would delete t74b41dm_OLD (created 2026-10-01 06:00Z, 30.0 h old)" in dry
    assert "leaving JORBO_USER (not a test prefix)" in dry
    assert "keeping t74b41dm_NEW (recent)" in dry
    assert "deleting t74b41dm_OLD" in applied and "would delete" not in applied


# --- the whole sweep against a fake broker ---------------------------------


class _Page:
    def __init__(self, items: list, total: int) -> None:
        self._items, self.total = items, total

    def __iter__(self):
        return iter(self._items)


class _FakeJetStream:
    """Serves its streams two at a time, as the server pages them, and records deletions."""

    def __init__(self, streams: list) -> None:
        self.streams = streams
        self.deleted: list[str] = []

    async def streams_info_iterator(self, offset: int = 0) -> _Page:
        return _Page(self.streams[offset : offset + 2], len(self.streams))

    async def delete_stream(self, name: str) -> bool:
        self.deleted.append(name)
        return True


class _FakeConnection:
    def __init__(self, js: _FakeJetStream) -> None:
        self._js = js
        self.closed = False

    def jetstream(self) -> _FakeJetStream:
        return self._js

    async def close(self) -> None:
        self.closed = True


def _fake_broker(monkeypatch, streams: list) -> tuple[_FakeJetStream, list[str]]:
    import nats

    js = _FakeJetStream(streams)
    dialled: list[str] = []

    async def connect(url: str, **_):
        dialled.append(url)
        return _FakeConnection(js)

    monkeypatch.setattr(nats, "connect", connect)
    return js, dialled


def _leftovers() -> list:
    return [
        stream("t74b41dm_A", 30),
        stream("JORBO_USER", 500),
        stream("KV_t74b41dm_bucket", 30),
        stream("t74b41dm_NEW", 1),
        stream("t74b41dm_C", 30),
    ]


async def test_a_dry_run_reads_every_page_and_deletes_nothing(monkeypatch, capsys):
    js, _ = _fake_broker(monkeypatch, _leftovers())
    monkeypatch.setattr(sweep_script, "datetime", SimpleNamespace(now=lambda tz: NOW))

    found = await sweep_script.sweep("nats://private:1", 6, apply=False)

    assert js.deleted == []
    assert names(found.stale) == ["KV_t74b41dm_bucket", "t74b41dm_A", "t74b41dm_C"]
    assert "would delete t74b41dm_C" in capsys.readouterr().out


async def test_apply_deletes_exactly_the_stale_ones(monkeypatch):
    js, _ = _fake_broker(monkeypatch, _leftovers())
    monkeypatch.setattr(sweep_script, "datetime", SimpleNamespace(now=lambda tz: NOW))

    await sweep_script.sweep("nats://private:1", 6, apply=True)

    assert sorted(js.deleted) == ["KV_t74b41dm_bucket", "t74b41dm_A", "t74b41dm_C"]


# --- the command line ------------------------------------------------------


def test_apply_with_no_named_address_refuses_and_dials_nothing(monkeypatch, capsys):
    monkeypatch.delenv("CLIFFRACER_TEST_NATS_URL", raising=False)
    _, dialled = _fake_broker(monkeypatch, _leftovers())

    code = sweep_script.main(["--apply"])

    assert code == 2 and dialled == []
    assert "refusing to delete on an address nobody named" in capsys.readouterr().err


def test_apply_takes_its_address_from_the_flag(monkeypatch):
    monkeypatch.delenv("CLIFFRACER_TEST_NATS_URL", raising=False)
    js, dialled = _fake_broker(monkeypatch, _leftovers())

    code = sweep_script.main(["--apply", "--older-than", "0", "--url", "nats://private:1"])

    assert code == 0 and dialled == ["nats://private:1"]
    assert "JORBO_USER" not in js.deleted and js.deleted


def test_apply_takes_its_address_from_the_environment(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_TEST_NATS_URL", "nats://private:2")
    _, dialled = _fake_broker(monkeypatch, _leftovers())

    assert sweep_script.main(["--apply", "--older-than", "0"]) == 0
    assert dialled == ["nats://private:2"]


def test_a_dry_run_with_no_named_address_reports_against_the_suites_default(monkeypatch):
    monkeypatch.delenv("CLIFFRACER_TEST_NATS_URL", raising=False)
    js, dialled = _fake_broker(monkeypatch, _leftovers())

    assert sweep_script.main([]) == 0
    assert dialled == [sweep_script._default_url()] and js.deleted == []

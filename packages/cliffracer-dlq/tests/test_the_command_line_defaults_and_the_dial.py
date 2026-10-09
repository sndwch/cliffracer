"""The command line's defaults and refusals, and the dial it makes.

`ls` lists 50 by default and accepts `--limit 1`; a usage error prints the usage line first.
`--since` counts back from now. `show` takes no filters and dials. The dial never reconnects.
`ls --json` writes each object with sorted keys.
"""

from __future__ import annotations

import datetime
import json

import pytest
from cliffracer_dlq import cli
from nats.js.api import RawStreamMsg

pytestmark = pytest.mark.unit

WHEN = datetime.datetime(2026, 10, 2, 16, 0, 0, tzinfo=datetime.UTC)


LIMIT = {
    "original_subject": "events.order",
    "error": "TypeError: unlucky\nsecond line",
    "service": "orders",
    "deliveries": 5,
    "stream": "EVENTS",
    "stream_sequence": 42,
}


def _dial_raises(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def dial(url, *, timeout, **options):
        calls.append(options)
        raise TimeoutError

    monkeypatch.setattr("cliffracer.core.dial.connect", dial)
    return calls


def test_ls_lists_fifty_by_default():
    assert cli.build_parser().parse_args(["ls"]).limit == 50


def test_a_usage_error_prints_the_usage_line(capsys):
    with pytest.raises(SystemExit):
        cli.main(["ls", "--limit", "0"])

    assert capsys.readouterr().err.startswith("usage: cliffracer-dlq")


def test_a_limit_of_one_is_accepted(monkeypatch):
    _dial_raises(monkeypatch)

    assert cli.main(["ls", "--limit", "1"]) == cli.EXIT_NO_BROKER


def test_since_is_counted_back_from_now(monkeypatch):
    _dial_raises(monkeypatch)

    assert cli.main(["count", "--since", "1h"]) == cli.EXIT_NO_BROKER


def test_show_takes_no_filters_and_dials(monkeypatch):
    calls = _dial_raises(monkeypatch)

    assert cli.main(["show", "1"]) == cli.EXIT_NO_BROKER
    assert len(calls) == 1


def test_the_dial_never_reconnects(monkeypatch):
    calls = _dial_raises(monkeypatch)

    cli.main(["count"])

    assert (calls[0]["max_reconnect_attempts"], calls[0]["allow_reconnect"]) == (0, False)


class _OneMessage:
    """A broker whose stream DLQ holds one dead letter."""

    def __init__(self, record):
        self.message = RawStreamMsg(
            subject="dlq.orders", seq=1, data=json.dumps(record).encode(), headers={}, time=WHEN
        )

    async def stream_names(self, subject):
        return ["DLQ"]

    async def stream_info(self, name):
        from types import SimpleNamespace

        return SimpleNamespace(state=SimpleNamespace(messages=1, first_seq=1, last_seq=1))

    async def get_msg(self, stream, seq=None, subject=None, direct=False, next=False):
        return self.message


def test_ls_json_writes_each_object_with_sorted_keys():
    import asyncio
    import io

    out, err = io.StringIO(), io.StringIO()
    args = cli.build_parser().parse_args(["ls", "--json"])

    asyncio.run(cli.execute(args, _OneMessage(LIMIT), out, err, now=lambda: WHEN))

    keys = list(json.loads(out.getvalue()))
    assert keys == sorted(keys)

"""Remove test-session streams and KV buckets the session that made them did not clean up.

A session deletes its own prefix at teardown. A session that was killed, timed
out, or crashed does not, and its streams stay until something removes them.
This is that something. It is run by hand.

Usage::

    python scripts/sweep_orphan_test_prefixes.py --url nats://host:4222 --older-than 6
    python scripts/sweep_orphan_test_prefixes.py --url nats://host:4222 --older-than 6 --apply

It reports by default and deletes only with ``--apply``, because it is the kind
of script whose first run should be read rather than trusted: it decides what is
a test prefix by NAME, and a real stream named like one would be indistinguishable.
The report lists what it would delete, with each one's age, and every name it is
leaving alone, so a reader can see that a named service stream is not on the list.

What it deletes: a stream or KV bucket whose name has the shape a test session's
prefix gives it AND whose age is known to be over the threshold. A stream whose
age the server did not report is kept, never deleted. ``--apply`` needs an
address named with ``--url`` or ``$CLIFFRACER_TEST_NATS_URL``; without one the
script reports against the suite's default address and refuses to delete there.

JetStream's ``max_age`` does not do this job: it expires a stream's *messages*,
not the stream, so an interrupted run's streams stay listed, empty.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# What this script is willing to delete: the shape session_prefix() produces,
# `t` then hex then a worker token. Anything else is somebody's real stream.
TEST_PREFIX = re.compile(r"^(KV_)?t[0-9a-f]{4,8}(m|gw\d+)_")


@dataclass
class Plan:
    """What a sweep would do with the streams it was shown."""

    stale: list[tuple[str, datetime]] = field(default_factory=list)
    young: list[str] = field(default_factory=list)
    unknown_age: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def plan(
    streams: Iterable[Any],
    now: datetime,
    older_than_hours: float,
    pattern: re.Pattern[str] = TEST_PREFIX,
) -> Plan:
    """Sort streams into delete, too young, age unknown, and not ours.

    Only the first is deleted. A stream the server gave no creation time is not
    assumed old: an unreadable age is a reason to leave it, not to remove it.
    """
    cutoff = now - timedelta(hours=older_than_hours)
    result = Plan()
    for info in streams:
        name = info.config.name
        if not pattern.match(name):
            result.kept.append(name)
            continue
        created = getattr(info, "created", None)
        if created is None:
            result.unknown_age.append(name)
        elif created > cutoff:
            result.young.append(name)
        else:
            result.stale.append((name, created))
    return result


def render(found: Plan, now: datetime, apply: bool) -> list[str]:
    """The report, one line each."""
    verb = "deleting" if apply else "would delete"
    lines = [
        f"{len(found.stale)} stale, {len(found.young)} recent, "
        f"{len(found.unknown_age)} of unknown age, {len(found.kept)} not test prefixes"
    ]
    for name, created in sorted(found.stale):
        hours = (now - created).total_seconds() / 3600
        lines.append(f"  {verb} {name} (created {created:%Y-%m-%d %H:%M}Z, {hours:.1f} h old)")
    lines += [f"  keeping {name} (age unknown)" for name in sorted(found.unknown_age)]
    lines += [f"  keeping {name} (recent)" for name in sorted(found.young)]
    lines += [f"  leaving {name} (not a test prefix)" for name in sorted(found.kept)]
    return lines


async def sweep(
    url: str,
    older_than_hours: float,
    apply: bool,
    pattern: re.Pattern[str] = TEST_PREFIX,
) -> Plan:
    import nats

    from cliffracer.core.jetstream import all_streams

    nc = await nats.connect(url, name="orphan-prefix-sweep")
    js = nc.jetstream()
    try:
        # Every page: a listing that stops at the first one cannot delete what it never saw.
        now = datetime.now(UTC)
        found = plan(await all_streams(js), now, older_than_hours, pattern)
        for line in render(found, now, apply):
            print(line)
        if apply:
            for name, _ in found.stale:
                await js.delete_stream(name)
    finally:
        await nc.close()
    return found


def _default_url() -> str:
    """The broker the suite uses, read from the config rather than pinned here.

    One address for the suite, and $CLIFFRACER_TEST_NATS_URL moves it; a literal
    here would be a second answer that stops agreeing the moment the first moves.
    """
    from cliffracer import ServiceConfig

    return str(ServiceConfig.model_fields["nats_url"].default)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--older-than", type=float, default=6.0, help="hours")
    parser.add_argument("--apply", action="store_true", help="delete rather than report")
    parser.add_argument("--url", default=os.environ.get("CLIFFRACER_TEST_NATS_URL"))
    args = parser.parse_args(argv)
    if args.apply and not args.url:
        print(
            "refusing to delete on an address nobody named: pass --url or set "
            "$CLIFFRACER_TEST_NATS_URL",
            file=sys.stderr,
        )
        return 2
    asyncio.run(sweep(args.url or _default_url(), args.older_than, args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

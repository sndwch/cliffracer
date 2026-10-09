"""Which dead letters a listing or a count looks at."""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from cliffracer.core.subjects import subject_matches
from cliffracer_dlq.records import DeadLetter

_DURATION = re.compile(r"(\d+)([dhms])")
_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> datetime.timedelta:
    """A run of numbers each followed by `d`, `h`, `m` or `s`, such as `90s` or `1d3h5m2s`; anything else is refused."""
    pieces = _DURATION.findall(text)
    if not text or "".join(f"{n}{u}" for n, u in pieces) != text:
        raise ValueError(f"{text!r} is not a duration such as 90s, 15m, 2h or 1d3h5m2s")
    seconds = sum(int(n) * _UNITS[u] for n, u in pieces)
    if seconds <= 0:
        raise ValueError(f"{text!r} is not a positive duration")
    return datetime.timedelta(seconds=seconds)


@dataclass(frozen=True)
class Filters:
    """A dead letter is kept when it satisfies every filter that is set.

    A message that could not be read as a record has no service, cause or original subject, so
    it satisfies none of those filters, and satisfies `since` by its time like any other.
    """

    service: str | None = None
    cause: str | None = None
    since: datetime.datetime | None = None
    original_subject: str | None = None

    def matches(self, dead_letter: DeadLetter) -> bool:
        if self.service is not None and dead_letter.service != self.service:
            return False
        if self.cause is not None and dead_letter.cause != self.cause:
            return False
        if self.original_subject is not None:
            original = dead_letter.original_subject
            if original is None or not subject_matches(self.original_subject, original):
                return False
        if self.since is not None:
            when = dead_letter.time
            if when is None or when < self.since:
                return False
        return True

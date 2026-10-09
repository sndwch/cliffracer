"""What the commands print."""

from __future__ import annotations

import datetime
import json
from collections import Counter
from collections.abc import Iterable
from typing import Any

from cliffracer_dlq.records import DeadLetter

_ERROR_WIDTH = 90


def _stamp(when: datetime.datetime | None) -> str:
    if when is None:
        return "-"
    return when.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: str | None, width: int = _ERROR_WIDTH) -> str:
    if not text:
        return "-"
    return text if len(text) <= width else text[: width - 1] + "…"


def summary(dead_letter: DeadLetter) -> dict[str, Any]:
    """The one-object-per-message form: what a listing says, in fields."""
    return {
        "sequence": dead_letter.sequence,
        "time": None if dead_letter.time is None else _stamp(dead_letter.time),
        "subject": dead_letter.subject,
        "cause": dead_letter.cause,
        "service": dead_letter.service,
        "original_subject": dead_letter.original_subject,
        "deliveries": dead_letter.deliveries,
        "error": dead_letter.error_line,
        "stream": dead_letter.field("stream"),
        "stream_sequence": dead_letter.field("stream_sequence"),
        "consumer": dead_letter.field("consumer"),
        "problem": dead_letter.problem,
    }


def list_line(dead_letter: DeadLetter) -> str:
    if dead_letter.record is None:
        return f"{dead_letter.sequence:>8}  {_stamp(dead_letter.time)}  unreadable  {_clip(dead_letter.problem)}"
    deliveries = "-" if dead_letter.deliveries is None else str(dead_letter.deliveries)
    return (
        f"{dead_letter.sequence:>8}  {_stamp(dead_letter.time)}  "
        f"{dead_letter.service or '-':<16} {dead_letter.cause or '-':<14} "
        f"{dead_letter.original_subject or '-'}  deliveries={deliveries}  "
        f"{_clip(dead_letter.error_line)}"
    )


def show_text(dead_letter: DeadLetter) -> str:
    lines = [
        f"sequence: {dead_letter.sequence}",
        f"time:     {_stamp(dead_letter.time)}",
        f"subject:  {dead_letter.subject}",
        f"cause:    {dead_letter.cause or 'unreadable'}",
    ]
    if dead_letter.problem:
        lines.append(f"problem:  {dead_letter.problem}")
    if dead_letter.headers:
        lines.append("headers:")
        lines.extend(f"  {name}: {value}" for name, value in sorted(dead_letter.headers.items()))
    if dead_letter.record is not None:
        lines.append("record:")
        lines.append(json.dumps(dead_letter.record, indent=2, sort_keys=True, default=str))
    stream = dead_letter.field("stream")
    original = dead_letter.field("stream_sequence")
    if isinstance(stream, str) and isinstance(original, int):
        lines.append(
            f"original message, while {stream} still holds it: nats stream get {stream} {original}"
        )
    return "\n".join(lines)


def show_json(dead_letter: DeadLetter) -> str:
    return json.dumps(
        {
            "sequence": dead_letter.sequence,
            "time": None if dead_letter.time is None else _stamp(dead_letter.time),
            "subject": dead_letter.subject,
            "headers": dict(dead_letter.headers),
            "record": None if dead_letter.record is None else dict(dead_letter.record),
            "problem": dead_letter.problem,
        },
        sort_keys=True,
        default=str,
    )


def count_table(dead_letters: Iterable[DeadLetter]) -> tuple[list[tuple[str, str, int]], int]:
    """Rows of (service, cause, count), most first, and the total."""
    counts: Counter[tuple[str, str]] = Counter()
    for dead_letter in dead_letters:
        counts[(dead_letter.service or "-", dead_letter.cause or "unreadable")] += 1
    rows = sorted(((s, c, n) for (s, c), n in counts.items()), key=lambda r: (-r[2], r[0], r[1]))
    return rows, sum(counts.values())


def count_text(rows: list[tuple[str, str, int]], total: int) -> str:
    lines = [f"{'service':<20} {'cause':<16} count"]
    lines.extend(f"{service:<20} {cause:<16} {count}" for service, cause, count in rows)
    lines.append(f"{'total':<37}{total}")
    return "\n".join(lines)

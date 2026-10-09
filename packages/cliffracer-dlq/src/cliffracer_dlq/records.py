"""A dead-letter message read back from its stream, and what it says about itself."""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cliffracer.core.validation import deserialize_payload

# The values the service writes in `cause` (`cliffracer.core.dispatch.dlq.CAUSE_*`). They are spelled
# here, not imported, so this package reads records from a service on any version of the framework:
# one that predates the field writes none, and the shape decides. A test holds the two spellings equal.
DECODE = "decode"
DELIVERY_LIMIT = "delivery-limit"
INVALID = "invalid"
CAUSES = (DECODE, DELIVERY_LIMIT, INVALID)

_DECODE_PREFIX = "Decode error:"


def classify(record: Mapping[str, Any]) -> str | None:
    """Which of the three dead-letter causes `record` has, or None when it has none.

    A record names its cause in `cause`. One published before that field existed has none, and
    its shape decides: a list of `errors` is a message that failed its schema, and `deliveries`
    is on the other two, where an `error` that starts with the decode prefix is a body that could
    not be decoded and any other is a handler that ran out of deliveries. Only such an old record
    is read by the text of `error`, which a handler's own exception can write. A `cause` that is
    not one of the three is ignored, and the shape decides.
    """
    cause = record.get("cause")
    if isinstance(cause, str) and cause in CAUSES:
        return cause
    if "errors" in record:
        return INVALID
    if "deliveries" in record:
        error = record.get("error")
        if isinstance(error, str) and error.startswith(_DECODE_PREFIX):
            return DECODE
        return DELIVERY_LIMIT
    return None


def _content_type(headers: Mapping[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value.split(";")[0].strip().lower()
    return None


@dataclass(frozen=True)
class DeadLetter:
    """One message of the dead-letter stream.

    `sequence` is its place in the dead-letter stream. `record` is the decoded record, or None
    when the message could not be read as one, in which case `problem` says why.
    """

    sequence: int
    time: datetime.datetime | None
    subject: str
    headers: Mapping[str, str]
    record: Mapping[str, Any] | None
    problem: str | None = None

    @classmethod
    def from_message(cls, message: Any) -> DeadLetter:
        """Read a stream message, never raising on what it holds."""
        headers = {str(k): str(v) for k, v in (getattr(message, "headers", None) or {}).items()}
        record: Mapping[str, Any] | None = None
        problem: str | None = None
        try:
            decoded = deserialize_payload(
                message.data or b"",
                content_type=_content_type(headers),
                fallback_format="json",
            )
        except Exception as exc:
            problem = f"cannot decode the message: {type(exc).__name__}: {exc}"
        else:
            if isinstance(decoded, Mapping):
                record = decoded
                if classify(record) is None:
                    record, problem = (
                        None,
                        "not a dead-letter record: no `errors` and no `deliveries`",
                    )
            else:
                problem = f"not a dead-letter record: the body is a {type(decoded).__name__}"
        return cls(
            sequence=int(message.seq),
            time=getattr(message, "time", None),
            subject=str(message.subject or ""),
            headers=headers,
            record=record,
            problem=problem,
        )

    @property
    def cause(self) -> str | None:
        return None if self.record is None else classify(self.record)

    def field(self, name: str) -> Any:
        return None if self.record is None else self.record.get(name)

    @property
    def service(self) -> str | None:
        value = self.field("service")
        return value if isinstance(value, str) else None

    @property
    def original_subject(self) -> str | None:
        value = self.field("original_subject")
        return value if isinstance(value, str) else None

    @property
    def deliveries(self) -> int | None:
        value = self.field("deliveries")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def error_line(self) -> str | None:
        """The first thing wrong, in one line: the `error`, or the first schema `errors` entry."""
        error = self.field("error")
        if isinstance(error, str):
            return error.splitlines()[0] if error else ""
        errors = self.field("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, Mapping):
                where = ".".join(str(part) for part in first.get("loc") or ()) or "-"
                text = f"{where}: {first.get('msg', first.get('type', '?'))}"
                return text if len(errors) == 1 else f"{text} (+{len(errors) - 1} more)"
        return None

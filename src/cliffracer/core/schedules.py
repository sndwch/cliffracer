"""Publishing a message at a later time, through JetStream message schedules.

`service.schedules.publish_at(subject, when=..., key=...)` publishes an event the broker writes
onto `subject` at `when`. The schedule is itself a stream message, on the schedule subject
`_sched.<subject>.<key>` (namespaced and prefixed as the target is), with the headers
`Nats-Schedule: @at <instant>` and `Nats-Schedule-Target: <subject>`. So it survives a restart of
every service and of the broker, and no service runs a timer for it. At the instant, the broker
writes a copy onto the target in the same stream, which a durable listener reads, and removes
the schedule. Publishing again under the same key replaces the schedule; `cancel` purges it.

This needs nats-server 2.12 or later, `jetstream_enabled`, and a declared stream that sets
`allow_msg_schedules=True` and covers both the target and its `_sched.` subject. Each missing
piece is refused by name with `MessageScheduleError`, before anything is sent where it can be
told, and from the server's `err_code` where only the server can tell.

The copy the broker writes carries the event's own headers but not its `Nats-Msg-Id`, which
deduplicates the scheduling publish, not the firing. A listener reads the copy at least once, as
any stream message: a handler that must act once keys on the `Nats-Scheduler` header, which
names the schedule subject and so the key, and is the same on every redelivery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nats.js.errors import APIError

from .discovery import HandlerDiscovery
from .exceptions import ConfigurationError, RpcError
from .jetstream import SCHEDULE_TOKEN, MessageScheduleError, stream_for_subject, subject_covered_by

if TYPE_CHECKING:
    from .service import CliffracerService

#: The oldest nats-server that runs message schedules.
FLOOR = (2, 12)

#: What the server's refusals of a schedule mean, by `err_code`.
_REFUSALS = {
    10188: "the stream does not allow message schedules (declare allow_msg_schedules=True)",
    10189: "the server could not read the schedule's time",
    10190: "the schedule's target is not in the same stream as the schedule",
}


class ScheduledPublishing:
    """A service's scheduled publishes: `publish_at`, `publish_in` and `cancel`."""

    def __init__(self, service: CliffracerService) -> None:
        self._service = service

    async def publish_at(
        self,
        subject: str,
        /,
        *,
        when: datetime,
        key: str,
        envelope: bool = True,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Publish an event on `subject` at `when`, an aware datetime, under the schedule `key`.

        The event is built as `publish_event` builds it. A `when` already past is written at
        once. Publishing again under the same `key` replaces the schedule.
        """
        from .service import event_envelope

        if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
            raise TypeError(f"when must be an aware datetime, an instant; got {when!r}")
        full_subject, schedule_subject = self._subjects(subject, key)
        self._check_the_broker(schedule_subject)
        payload, domain_data, cid = event_envelope(
            self._service.config.name, kwargs, envelope=envelope
        )
        instant = when.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        headers = {"Nats-Schedule": f"@at {instant}", "Nats-Schedule-Target": full_subject}
        self._service.logger.info(
            f"Scheduling event {subject} for {instant} under key {key!r} with correlation_id: {cid}"
        )
        try:
            return await self._service._send_event(
                full_subject,
                payload,
                domain_data,
                cid,
                idempotency_key=idempotency_key,
                schedule=(schedule_subject, headers),
            )
        except (APIError, RpcError) as exc:
            refusal = _a_schedule_refusal(exc)
            if refusal is None:
                raise
            raise MessageScheduleError(
                f"{schedule_subject!r} was not scheduled: {_REFUSALS[refusal]} "
                f"(err_code={refusal})",
                {"err_code": refusal},
            ) from exc

    async def publish_in(
        self, subject: str, /, *, after: timedelta, key: str, **kwargs: Any
    ) -> Any:
        """Publish an event on `subject` `after` from now, under the schedule `key`.

        The broker fires on an instant, so `after` is turned into one here, on this host's clock.
        """
        if not isinstance(after, timedelta) or after < timedelta(0):
            raise ValueError(f"after must be a timedelta of zero or more; got {after!r}")
        return await self.publish_at(subject, when=datetime.now(UTC) + after, key=key, **kwargs)

    async def cancel(self, subject: str, /, *, key: str) -> None:
        """Remove the schedule `key` for `subject`, if it has not fired; nothing is written."""
        _, schedule_subject = self._subjects(subject, key)
        spec = stream_for_subject(
            self._service.config.effective_jetstream_streams, schedule_subject
        )
        js = self._service.js
        if js is None:
            raise MessageScheduleError(
                f"{schedule_subject!r} cannot be cancelled: the service has no JetStream context "
                f"(it is not connected)"
            )
        await js.purge_stream(spec.name, subject=schedule_subject)

    def _subjects(self, subject: str, key: str) -> tuple[str, str]:
        """The target and the schedule subject, both namespaced and prefixed, after the checks
        that need no broker: JetStream on, a key that is one subject token, and one declared
        stream that allows schedules and covers both."""
        config = self._service.config
        if not config.jetstream_enabled:
            raise ConfigurationError(
                "scheduling a message needs jetstream_enabled: a schedule is a stream message, "
                "and core NATS has nothing to hold it"
            )
        if (
            not isinstance(key, str)
            or not key
            or any(c in key for c in ".*> \t\r\n")
            or not key.isprintable()
        ):
            raise ValueError(
                f"key must be one subject token (no '.', '*', '>' or white space); got {key!r}"
            )
        full_subject = HandlerDiscovery.with_namespace(config, subject)
        schedule_subject = HandlerDiscovery.with_namespace(
            config, f"{SCHEDULE_TOKEN}.{subject}.{key}"
        )
        declared = config.effective_jetstream_streams
        for needed in (full_subject, schedule_subject):
            if not subject_covered_by(declared, needed):
                raise MessageScheduleError(
                    f"no declared stream covers {needed!r}: a scheduled event needs one stream "
                    f"with allow_msg_schedules=True that covers both {full_subject!r} and "
                    f"{schedule_subject!r}"
                )
        target, holder = (
            stream_for_subject(declared, full_subject),
            stream_for_subject(declared, schedule_subject),
        )
        if target.name != holder.name:
            raise MessageScheduleError(
                f"{full_subject!r} is in stream {target.name!r} and its schedule "
                f"{schedule_subject!r} in {holder.name!r}: the broker writes a scheduled message "
                f"only within the stream that holds the schedule"
            )
        if not holder.allow_msg_schedules:
            raise MessageScheduleError(
                f"stream {holder.name!r} does not allow message schedules: declare it with "
                f"allow_msg_schedules=True"
            )
        return full_subject, schedule_subject

    def _check_the_broker(self, schedule_subject: str) -> None:
        """Refuse a broker too old for message schedules, which would store the schedule as a
        plain message and never fire it."""
        nc = self._service.nc
        version = getattr(nc, "connected_server_version", None)
        major, minor = getattr(version, "major", None), getattr(version, "minor", None)
        if isinstance(major, int) and isinstance(minor, int) and (major, minor) < FLOOR:
            raise MessageScheduleError(
                f"{schedule_subject!r} was not scheduled: the broker is nats-server "
                f"{major}.{minor}, and message schedules need {FLOOR[0]}.{FLOOR[1]} or later"
            )


def _a_schedule_refusal(error: BaseException) -> int | None:
    """The `err_code` of a server refusal of a schedule behind `error`, or None."""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, APIError) and cause.err_code in _REFUSALS:
            return int(cause.err_code)
        cause = cause.__cause__
    return None

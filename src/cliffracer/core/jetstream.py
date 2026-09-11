"""JetStream declaration and consumer configuration.

Everything here is pure or takes its JetStream context as an argument, so it
needs no service state and is testable without a broker. The parts that need
service state — the ack policy, the DLQ assertion — live on
``CliffracerService``.
"""

from typing import Any, Literal

from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StorageType, StreamConfig
from pydantic import BaseModel, Field

from .exceptions import ConfigurationError
from .subjects import subject_matches, subjects_overlap


class StreamDeclarationError(ConfigurationError):
    """A stream declaration is missing or cannot be satisfied.

    This is a stream-declaration defect: the service asked for something its
    ``jetstream_streams`` do not provide for. It surfaces at startup for
    provisioning conflicts and DLQ-coverage gaps, and at first publish when a
    subject has no declared stream — whichever the defect is caught at, the
    fix is always to change the declaration.
    """


class StreamSpec(BaseModel):
    """A JetStream stream a service declares at startup.

    ``subjects`` are literal and are NEVER namespace-prefixed for you. A
    subject may be claimed by exactly one stream, and on a broker shared
    between projects an inferred claim is a claim nobody wrote down.

    **A stream subject must not begin with a wildcard** on nats-server 2.10.29
    and later. ``*`` can match ``$JS``, so a leading ``*`` counts as
    overlapping the JetStream API subject space and the server refuses the
    stream with ``err_code=10052``, whose text mentions no-ack and the
    JetStream API and mentions neither wildcards nor a version. This tightened
    somewhere after 2.10.22, which accepted ``*.events.thing.*`` happily --
    so an older broker will contradict this docstring.

    That matters most for a ``cross_namespace=True`` listener, which resolves
    to ``*.<pattern>``. **Do not claim that shape.** Enumerate the namespaces
    instead::

        # listener: @listener("events.thing", cross_namespace=True)
        StreamSpec(name="THING", subjects=["jorbo.events.thing",
                                           "utils.events.thing"])

    The listener does not change. A CONSUMER may lead with a wildcard even
    though a STREAM may not, and that asymmetry is what keeps cross-namespace
    subscription working against an enumerated stream.
    """

    name: str
    subjects: list[str] = Field(min_length=1)
    storage: Literal["file", "memory"] = "file"
    retention: Literal["limits", "interest", "workqueue"] = "limits"
    max_age_seconds: float | None = None
    duplicate_window_seconds: float = 120.0

    def to_stream_config(self) -> StreamConfig:
        """The nats-py config this declaration asks the server for."""
        return StreamConfig(
            name=self.name,
            subjects=list(self.subjects),
            storage=StorageType(self.storage),
            retention=RetentionPolicy(self.retention),
            max_age=self.max_age_seconds,
            duplicate_window=self.duplicate_window_seconds,
        )

    @staticmethod
    def _plain(value: Any, default: str) -> str:
        """Normalise a storage/retention value to its plain string form.

        A config built locally by ``to_stream_config`` carries real
        ``StorageType``/``RetentionPolicy`` enum members. A config that has
        round-tripped through the server (``js.streams_info()``) carries
        plain strings instead — nats-py does not re-hydrate these fields
        into enums on the way back. Both shapes have to compare equal.
        """
        if value is None:
            return default
        return str(getattr(value, "value", value))

    def matches(self, config: StreamConfig) -> bool:
        """True if a stream already on the server is what this declaration asks for.

        Subject order is not significant. Server-side defaults are compared
        against our own defaults rather than against ``None``, because a
        server round-trip fills fields we left unset.
        """
        storage = self._plain(config.storage, "file")
        retention = self._plain(config.retention, "limits")
        server_dup_win = getattr(config, "duplicate_window", None)
        spec_dup_win = self.duplicate_window_seconds or 0
        if server_dup_win in (None, 0, 120.0) and spec_dup_win in (0, 120.0):
            dup_matches = True
        else:
            dup_matches = (server_dup_win or 0) == spec_dup_win
        return (
            config.name == self.name
            and set(config.subjects or []) == set(self.subjects)
            and storage == self.storage
            and retention == self.retention
            and (config.max_age or 0) == (self.max_age_seconds or 0)
            and dup_matches
        )


def subject_covered_by(specs: list[StreamSpec], subject: str) -> bool:
    """True if any declared stream claims this concrete subject."""
    return any(subject_matches(pattern, subject) for spec in specs for pattern in spec.subjects)


def consumer_config_for(config: Any) -> ConsumerConfig:
    """The durable push consumer this service's tuning asks for.

    ``config`` is a ``ServiceConfig``; it is untyped here to avoid a circular
    import between this module and ``service_config``.
    """
    return ConsumerConfig(
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=config.jetstream_ack_wait,
        max_deliver=config.jetstream_max_deliver,
        max_ack_pending=config.jetstream_max_ack_pending,
    )


#: The consumer fields ``consumer_config_for`` sets, and which a pre-existing
#: durable therefore pins. Kept beside it so the two cannot drift apart.
TUNED_CONSUMER_FIELDS = ("ack_policy", "ack_wait", "max_deliver", "max_ack_pending")


def consumer_config_drift(asked: ConsumerConfig, actual: ConsumerConfig) -> list[tuple]:
    """Fields where a live durable disagrees with what this service asked for.

    A durable consumer's configuration is fixed when it is created. nats-py
    adopts an existing consumer's config wholesale rather than applying the one
    passed to ``subscribe``, so edits to the tuning fields on ``ServiceConfig``
    are accepted in-process and ignored by the server, with nothing raised.

    Returns ``(field, asked, actual)`` triples, empty when they agree.
    """
    drift = []
    for field in TUNED_CONSUMER_FIELDS:
        want = getattr(asked, field, None)
        have = getattr(actual, field, None)
        if want is not None and want != have:
            drift.append((field, want, have))
    return drift


def nak_delay(num_delivered: int, config: Any) -> float:
    """Exponential backoff for a nak, capped.

    ``num_delivered`` is 1 on first delivery, so the first nak waits the base
    delay rather than double it.
    """
    exponent = max(num_delivered - 1, 0)
    return float(min(config.jetstream_nak_backoff * (2**exponent), config.jetstream_max_backoff))


def _assert_no_overlap(spec: StreamSpec, others: dict[str, list[str]]) -> None:
    """Refuse a declaration that would claim a subject another stream owns.

    The server rejects this too, with "10054 subjects overlap with an existing
    stream" and no indication of which stream or which subject. Catching it
    here is the difference between a fixable error and a 3am one.
    """
    for other_name, other_subjects in others.items():
        for mine in spec.subjects:
            for theirs in other_subjects:
                if subjects_overlap(mine, theirs):
                    raise StreamDeclarationError(
                        f"stream {spec.name!r} claims {mine!r}, which overlaps "
                        f"{theirs!r} already claimed by stream {other_name!r}. "
                        f"A subject may be claimed by exactly one stream — narrow "
                        f"one of the two claims."
                    )


async def ensure_streams(
    js: Any,
    specs: list[StreamSpec],
    *,
    allow_update: bool = False,
    logger: Any = None,
) -> None:
    """Declare each stream idempotently, or refuse and say why.

    Absent           -> add.
    Present, same    -> no-op, so a publisher and a consumer can both declare it.
    Present, differs -> raise, unless allow_update.

    Refuse-and-report is the default because two services declaring one stream
    name with different subject sets would otherwise rewrite it on every boot,
    flapping a stream that carries live traffic.
    """
    if not specs:
        return

    infos = await js.streams_info()
    claims: dict[str, list[str]] = {i.config.name: list(i.config.subjects or []) for i in infos}
    server_configs = {i.config.name: i.config for i in infos}

    for spec in specs:
        current = server_configs.get(spec.name)
        others = {name: subjects for name, subjects in claims.items() if name != spec.name}

        if current is None:
            _assert_no_overlap(spec, others)
            await js.add_stream(config=spec.to_stream_config())
            claims[spec.name] = list(spec.subjects)
            if logger:
                logger.info(f"Declared JetStream stream {spec.name!r} for {spec.subjects}")
            continue

        if spec.matches(current):
            claims[spec.name] = list(spec.subjects)
            continue

        if not allow_update:
            raise StreamDeclarationError(
                f"stream {spec.name!r} already exists with subjects "
                f"{sorted(current.subjects or [])}, but this service declares "
                f"{sorted(spec.subjects)}. Refusing to rewrite a stream that another "
                f"service may own. Set jetstream_update_streams=True to apply the "
                f"change deliberately, or align the declarations."
            )

        _assert_no_overlap(spec, others)
        await js.update_stream(config=spec.to_stream_config())
        claims[spec.name] = list(spec.subjects)
        if logger:
            logger.info(
                f"Updated JetStream stream {spec.name!r}: "
                f"{sorted(current.subjects or [])} -> {sorted(spec.subjects)}"
            )

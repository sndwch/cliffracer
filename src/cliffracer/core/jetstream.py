"""JetStream declaration and consumer configuration.

Everything here is pure or takes its JetStream context as an argument, so it
needs no service state and is testable without a broker. The parts that need
service state — the ack policy, the DLQ assertion — live on
``CliffracerService``.
"""

import math
from typing import Any, Literal

from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import NotFoundError
from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from .exceptions import ConfigurationError
from .subjects import subject_matches, subjects_overlap

#: The subject token a schedule is published under: `_sched.<target subject>.<key>`.
SCHEDULE_TOKEN = "_sched"


class StreamDeclarationError(ConfigurationError):
    """A stream declaration is missing or cannot be satisfied.

    This is a stream-declaration defect: the service asked for something its
    ``jetstream_streams`` do not provide for. It surfaces at startup for
    provisioning conflicts and DLQ-coverage gaps, and at first publish when a
    subject has no declared stream — whichever the defect is caught at, the
    fix is always to change the declaration.
    """


class MessageScheduleError(StreamDeclarationError):
    """A message cannot be scheduled: the broker, or the declaration, cannot hold the schedule.

    Message schedules need nats-server 2.12 or later, a stream that declares
    ``allow_msg_schedules=True``, and a schedule subject in the same stream as its target. The
    message says which of them is missing; ``details`` carries the server's ``err_code`` when the
    server refused the schedule.
    """


class ConsumerBindingError(ConfigurationError):
    """A durable consumer required by bind mode is absent or incompatible."""


# What nats-py refuses in a stream name, plus any white space (see `declaration_problem`).
_ILLEGAL_STREAM_NAME_CHARS = ">*./\\"


def _subject_problem(subject: Any) -> str | None:
    """Why the server refuses `subject` as a stream subject, or None."""
    if not isinstance(subject, str) or not subject:
        return "is empty"
    if any(ord(char) < 33 or ord(char) == 127 or char.isspace() for char in subject):
        return "holds white space or a control character"
    tokens = subject.split(".")
    if any(not token for token in tokens):
        return "has an empty token"
    if ">" in tokens[:-1]:
        return "has a '>' that is not its last token"
    return None


# The window the server applies to a stream that does not set one, when the stream keeps messages
# at least that long.
_DEFAULT_DUPLICATE_WINDOW = 120.0


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

    ``duplicate_window_seconds`` is the window in which the server drops a message that repeats a
    message id it has stored, ``120.0`` by default. ``0`` does not turn deduplication off: the
    server stores a zero window as its default and reports ``120.0`` back, so a declared ``0`` gets
    the two-minute window, and reconciliation reads a server ``0``, ``None`` or ``120.0`` as that
    same window. The server refuses a window longer than the stream's age, so with a
    ``max_age_seconds`` under 120 the default window, left out or ``0``, is that age, and a window
    set longer than the age is refused when the declaration is built. A dump of a declaration
    that left the window out leaves it out, so validating the dump gives the same declaration.
    """

    name: str
    subjects: list[str] = Field(min_length=1)
    storage: Literal["file", "memory"] = "file"
    retention: Literal["limits", "interest", "workqueue"] = "limits"
    max_age_seconds: float | None = None
    duplicate_window_seconds: float = _DEFAULT_DUPLICATE_WINDOW
    #: Whether a message published to this stream may carry a schedule for the broker to fire
    #: later (nats-server 2.12 and later). A stream that allows it also declares a subject in the
    #: `_sched.` branch, where `CliffracerService.schedules` publishes the schedules.
    allow_msg_schedules: bool = False
    #: The most messages the stream holds, and the most bytes. Left out, the stream has no such
    #: limit when created and keeps whatever limit an operator gave it when updated or bound.
    max_msgs: int | None = None
    max_bytes: int | None = None
    #: What the server does at a limit: ``"old"`` drops the oldest message, ``"new"`` refuses the
    #: new one. Left out, ``"old"`` on creation and the operator's choice otherwise.
    discard: Literal["old", "new"] | None = None
    #: The fewest copies of the stream a cluster keeps, 1 to 5: more is not drift. Left out, one on
    #: creation and the operator's choice otherwise.
    num_replicas: int | None = None

    @field_validator("subjects")
    @classmethod
    def _refuse_a_subject_that_begins_with_a_wildcard(cls, subjects: list[str]) -> list[str]:
        """The shape the server refuses, caught where it is declared.

        A first token of ``>``, or of ``*`` with more tokens after it, can match
        ``$JS``, so the server answers ``err_code=10052`` with text about no-ack
        and the JetStream API. A lone ``*`` has no second token to overlap with
        and is accepted, as are wildcards after the first token.
        """
        for subject in subjects:
            first, _, rest = subject.partition(".")
            if first == ">" or (first == "*" and rest):
                raise ValueError(
                    f"stream subject {subject!r} begins with a wildcard, which nats-server "
                    f"refuses (err_code=10052, 'subjects that overlap with jetstream api "
                    f"require no-ack to be true'): the wildcard can match $JS. Name the "
                    f"first token, or enumerate the namespaces the stream covers."
                )
        return subjects

    @model_validator(mode="after")
    def _refuse_a_declaration_the_client_or_server_would(self) -> "StreamSpec":
        problem = self.declaration_problem()
        if problem is not None:
            raise ValueError(f"stream {self.name!r} cannot be declared: {problem}")
        return self

    def declaration_problem(self) -> str | None:
        """Why the client or the server would refuse this declaration, or None.

        Both refuse at the moment a stream is added, which is after the streams before it in a
        list were created, so a value found here is found before any is. The checks are the ones
        they make: a name nats-py refuses (empty, or holding a wildcard, a dot, a separator or
        white space), a subject the server refuses (an empty token, white space or a control
        character, a ``>`` that is not the last token, two of them that overlap or repeat), and a duration that is not a finite
        number of seconds of zero or more. A declaration is checked again where it is applied
        because assigning to a field, or ``model_construct``, skips the validation here.
        """
        name = self.name
        if not isinstance(name, str) or not name:
            return "the name is empty"
        illegal = sorted({c for c in name if c in _ILLEGAL_STREAM_NAME_CHARS or c.isspace()})
        if illegal:
            return (
                f"the name holds {''.join(illegal)!r}, which nats-py refuses in a stream name "
                f"(no wildcard, dot, slash, backslash or white space)"
            )
        for subject in self.subjects:
            problem = _subject_problem(subject)
            if problem is not None:
                return f"subject {subject!r} {problem}"
        for index, subject in enumerate(self.subjects):
            for other in self.subjects[index + 1 :]:
                if subjects_overlap(subject, other):
                    return (
                        f"subjects {subject!r} and {other!r} overlap, and the server refuses a "
                        f"stream whose own subjects overlap or repeat"
                    )
        if self.allow_msg_schedules and not any(
            subject.split(".")[0] == SCHEDULE_TOKEN or f".{SCHEDULE_TOKEN}." in f".{subject}"
            for subject in self.subjects
        ):
            return (
                f"it allows message schedules but declares no subject with a "
                f"{SCHEDULE_TOKEN!r} token, where a schedule is published: add "
                f"'{SCHEDULE_TOKEN}.<subject>' beside each subject it schedules onto"
            )
        for field in ("max_age_seconds", "duplicate_window_seconds"):
            seconds = getattr(self, field)
            if seconds is not None and (not math.isfinite(seconds) or seconds < 0):
                return f"{field} is {seconds!r}; it is a finite number of seconds, 0 or more"
        for field, unit in (("max_msgs", "messages"), ("max_bytes", "bytes")):
            limit = getattr(self, field)
            if limit is not None and (
                isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
            ):
                return (
                    f"{field} is {limit!r}; it is a whole number of {unit}, 1 or more. The server "
                    f"reads 0 and -1 as no limit, so leave it out for none"
                )
        replicas = self.num_replicas
        if replicas is not None and (
            isinstance(replicas, bool) or not isinstance(replicas, int) or not 1 <= replicas <= 5
        ):
            return f"num_replicas is {replicas!r}; a stream keeps 1 to 5 copies"
        if self.discard == "new" and self.max_msgs is None and self.max_bytes is None:
            return (
                "discard='new' refuses a message only once max_msgs or max_bytes is reached, and "
                "neither is declared, so it would change nothing: declare a limit, or leave "
                "discard out"
            )
        return None

    @model_validator(mode="after")
    def _refuse_a_window_longer_than_the_age(self) -> "StreamSpec":
        window, age = self.duplicate_window_seconds, self.max_age_seconds
        if self._sets_a_window() and window and age and window > age:
            raise ValueError(
                f"stream {self.name!r}: duplicate_window_seconds is {window!r}, longer than "
                f"max_age_seconds {age!r}, and the server refuses a window longer than the age. "
                f"Set the window to at most the age, or leave it out to use the age."
            )
        return self

    def _sets_a_window(self) -> bool:
        """Whether the declaration names a window, as against taking the default one."""
        return "duplicate_window_seconds" in self.model_fields_set

    @model_serializer(mode="wrap")
    def _leave_out_a_window_that_was_not_set(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """A dump holds the window only when the declaration set one.

        Whether the window was set decides what is asked of the server and whether a window longer
        than the age is refused. A dump that wrote the default two minutes for a window nobody set
        would validate to a declaration that set it, and a stream with a `max_age_seconds` under
        120 would be refused by its own dump.
        """
        dumped: dict[str, Any] = handler(self)
        if not self._sets_a_window():
            dumped.pop("duplicate_window_seconds", None)
        return dumped

    def default_duplicate_window(self) -> float:
        """The window the server stores for a declaration that does not set one.

        Two minutes, or the age of the stream when that is shorter: the server refuses a window
        longer than the age, and applies the age as the window when none is sent.
        """
        age = self.max_age_seconds
        return min(_DEFAULT_DUPLICATE_WINDOW, age) if age else _DEFAULT_DUPLICATE_WINDOW

    def effective_duplicate_window(self) -> float:
        """The window this declaration asks for: the one it sets, else the default."""
        if self._sets_a_window() and self.duplicate_window_seconds:
            return self.duplicate_window_seconds
        return self.default_duplicate_window()

    def to_stream_config(self) -> StreamConfig:
        """The nats-py config this declaration asks the server for."""
        return StreamConfig(
            name=self.name,
            subjects=list(self.subjects),
            storage=StorageType(self.storage),
            retention=RetentionPolicy(self.retention),
            max_age=self.max_age_seconds,
            duplicate_window=self.effective_duplicate_window(),
            # Sent only when asked for: a broker before 2.12 is never sent a field it lacks.
            allow_msg_schedules=True if self.allow_msg_schedules else None,
            **self._declared_limits(),
        )

    def apply_to(self, config: StreamConfig) -> StreamConfig:
        """Overlay declared fields onto a broker config, preserving every other field."""
        return config.evolve(
            name=self.name,
            subjects=list(self.subjects),
            storage=StorageType(self.storage),
            retention=RetentionPolicy(self.retention),
            max_age=self.max_age_seconds,
            duplicate_window=self.effective_duplicate_window(),
            allow_msg_schedules=True if self.allow_msg_schedules else config.allow_msg_schedules,
            **self._declared_limits(),
        )

    def _declared_limits(self) -> dict[str, Any]:
        """The limit, discard and replica fields this declaration sets, as nats-py names them.
        One it leaves out is not sent, so a new stream takes the server's default and an existing
        one keeps the operator's value."""
        declared: dict[str, Any] = {}
        if self.max_msgs is not None:
            declared["max_msgs"] = self.max_msgs
        if self.max_bytes is not None:
            declared["max_bytes"] = self.max_bytes
        if self.discard is not None:
            declared["discard"] = DiscardPolicy(self.discard)
        if self.num_replicas is not None:
            declared["num_replicas"] = self.num_replicas
        return declared

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

    def matches_declared_fields(self, config: StreamConfig) -> bool:
        """True when every field this declaration owns matches the broker.

        Subject order is not significant. Server-side defaults are compared
        against our own defaults rather than against ``None``, because a
        server round-trip fills fields we left unset.

        ``max_msgs``, ``max_bytes``, ``discard`` and ``num_replicas`` are compared
        only where declared. Placement, description, metadata, per-subject limits and
        fields added by newer brokers are outside ``StreamSpec``, and updates preserve
        them, with any limit left undeclared, from the broker's live configuration.
        """
        return not self.declared_differences(config)

    def declared_differences(self, config: StreamConfig) -> list[tuple[str, Any, Any]]:
        """Declared values that disagree, as ``(field, expected, actual)`` tuples."""
        actual = {
            "name": config.name,
            "subjects": sorted(config.subjects or []),
            "storage": self._plain(config.storage, "file"),
            "retention": self._plain(config.retention, "limits"),
            "max_age_seconds": config.max_age or 0,
        }
        expected = {
            "name": self.name,
            "subjects": sorted(self.subjects),
            "storage": self.storage,
            "retention": self.retention,
            "max_age_seconds": self.max_age_seconds or 0,
        }
        differences = [
            (field, expected[field], actual[field])
            for field in expected
            if expected[field] != actual[field]
        ]
        server_dup_win = getattr(config, "duplicate_window", None)
        # NATS applies its 120-second default when a declaration sends zero,
        # then reports 120 on the stored stream. Older/partial wire shapes may
        # report that same default as zero or omit it. Normalize both sides so
        # every representation of the server default remains idempotent while
        # genuine non-default windows still drift.
        spec_dup_win = self.effective_duplicate_window()
        actual_dup_win = (
            self.default_duplicate_window() if server_dup_win in (None, 0) else server_dup_win
        )
        if actual_dup_win != spec_dup_win:
            differences.append(("duplicate_window_seconds", spec_dup_win, actual_dup_win))
        # Asked for and not held: a broker before 2.12 ignores the field and reports none. A stream
        # that allows schedules when the declaration does not ask is left as it is.
        held = bool(getattr(config, "allow_msg_schedules", None))
        if self.allow_msg_schedules and not held:
            differences.append(("allow_msg_schedules", True, held))
        # As each broker reports them (measured on 2.10.29, 2.11.2 and 2.12.0): a limit left out,
        # sent as 0 or as -1 comes back as -1, no discard comes back "old", and no replica count, or
        # 0, comes back 1.
        for field in ("max_msgs", "max_bytes"):
            limit = getattr(self, field)
            if limit is None:
                continue
            reported = getattr(config, field, None)
            held_limit = reported if isinstance(reported, int) and reported > 0 else "no limit"
            if held_limit != limit:
                differences.append((field, limit, held_limit))
        if self.discard is not None:
            held_discard = self._plain(getattr(config, "discard", None), "old")
            if held_discard != self.discard:
                differences.append(("discard", self.discard, held_discard))
        # A replica count is a floor: more copies than declared is not drift, and lowering an
        # operator's count is not this service's to do.
        if self.num_replicas is not None:
            held_replicas = getattr(config, "num_replicas", None) or 1
            if held_replicas < self.num_replicas:
                differences.append(("num_replicas", self.num_replicas, held_replicas))
        return differences

    def replica_shortfall(self, info: Any) -> str | None:
        """Why the stream keeps fewer copies than declared, or None.

        A stream's configured replica count is not proof of copies: a single server refuses more
        than one when a stream is created, but accepts the change on an update and reports it
        back while keeping one copy (measured on 2.10.29, 2.11.2 and 2.12.0). So the copies are
        counted from the cluster information as well: the leader and every peer it lists,
        current or catching up, since a lagging peer is a copy being made.
        """
        if self.num_replicas is None:
            return None
        configured = getattr(info.config, "num_replicas", None) or 1
        cluster = getattr(info, "cluster", None)
        clustered = cluster is not None and getattr(cluster, "name", None) is not None
        live = 1 + len(getattr(cluster, "replicas", None) or []) if cluster is not None else 1
        if configured >= self.num_replicas and live >= self.num_replicas:
            return None
        where = "" if clustered else ", not clustered"
        return (
            f"num_replicas declared {self.num_replicas}: configured {configured}, {live} live"
            f"{where}"
        )

    def limit_below_usage(self, state: Any) -> list[tuple[str, int, int]]:
        """Each declared ``max_msgs`` or ``max_bytes`` lower than what the stream holds, as
        ``(field, declared, held)``: applying it would make the server drop stored messages."""
        held = {"max_msgs": getattr(state, "messages", 0), "max_bytes": getattr(state, "bytes", 0)}
        return [
            (field, limit, held[field])
            for field in ("max_msgs", "max_bytes")
            if (limit := getattr(self, field)) is not None
            and isinstance(held[field], int)
            and limit < held[field]
        ]


def subject_covered_by(specs: list[StreamSpec], subject: str) -> bool:
    """True if any declared stream claims this concrete subject."""
    return any(subject_matches(pattern, subject) for spec in specs for pattern in spec.subjects)


def stream_for_subject(specs: list[StreamSpec], subject: str) -> StreamSpec:
    """Return the one declared stream whose subjects overlap a listener subject."""
    matches = [
        spec
        for spec in specs
        if any(subjects_overlap(pattern, subject) for pattern in spec.subjects)
    ]
    if len(matches) != 1:
        raise StreamDeclarationError(
            f"listener subject {subject!r} needs exactly one declared stream; "
            f"found {[spec.name for spec in matches]}"
        )
    return matches[0]


def _subject_is_below(prefix: str, subject: str) -> bool:
    """Whether a concrete subject has the prefix's complete tokens plus a child token."""
    prefix_tokens = prefix.split(".")
    subject_tokens = subject.split(".")
    return len(subject_tokens) > len(prefix_tokens) and subject_tokens[: len(prefix_tokens)] == (
        prefix_tokens
    )


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


async def validate_bound_consumer(
    js: Any,
    *,
    stream: str,
    durable: str,
    subject: str,
    pull: bool,
    config: Any,
) -> Any:
    """Read and validate a durable before nats-py binds a subscription to it."""
    try:
        info = await js.consumer_info(stream, durable)
    except NotFoundError as exc:
        raise ConsumerBindingError(
            f"pre-provisioned durable consumer {durable!r} was not found on stream "
            f"{stream!r}. Create it with filter subject {subject!r} before starting "
            "this service in JetStream bind mode."
        ) from exc

    actual = info.config
    differences = [
        f"{field} is {have!r}, expected {want!r}"
        for field, want, have in consumer_config_drift(consumer_config_for(config), actual)
    ]
    actual_durable = getattr(actual, "durable_name", None)
    if actual_durable != durable:
        differences.append(
            f"durable_name is {actual_durable!r}, expected {durable!r} for a durable consumer"
        )
    filters = list(getattr(actual, "filter_subjects", None) or [])
    if not filters and getattr(actual, "filter_subject", None):
        filters = [actual.filter_subject]
    if filters != [subject]:
        differences.append(f"filter subjects are {filters!r}, expected {[subject]!r}")

    deliver_subject = getattr(actual, "deliver_subject", None)
    deliver_group = getattr(actual, "deliver_group", None)
    if pull:
        if deliver_subject is not None:
            differences.append(
                f"deliver_subject is {deliver_subject!r}, expected none for a pull consumer"
            )
        if deliver_group is not None:
            differences.append(
                f"deliver_group is {deliver_group!r}, expected none for a pull consumer"
            )
    else:
        if not deliver_subject:
            differences.append("deliver_subject is absent, expected a push consumer")
        elif config.nats_inbox_prefix and not _subject_is_below(
            config.nats_inbox_prefix, deliver_subject
        ):
            differences.append(
                f"deliver_subject is {deliver_subject!r}, expected it under the service "
                f"inbox prefix {config.nats_inbox_prefix!r}"
            )
        if deliver_group != durable:
            differences.append(f"deliver_group is {deliver_group!r}, expected {durable!r}")

    if differences:
        detail = "; ".join(differences)
        raise ConsumerBindingError(
            f"pre-provisioned durable consumer {durable!r} on stream {stream!r} "
            f"does not match this listener: {detail}. Recreate or update the consumer "
            "before starting this service in JetStream bind mode."
        )
    return info


def nak_delay(num_delivered: int, config: Any) -> float:
    """Exponential backoff for a nak, capped.

    ``num_delivered`` is 1 on first delivery, so the first nak waits the base
    delay rather than double it.
    """
    # The exponent is capped before the power is taken: an integer past 2**1023 does not convert to
    # a float, so a message on its thousandth redelivery raised `OverflowError` here, and a
    # `jetstream_max_deliver` has no upper bound. 2**63 times the base is far past any cap.
    exponent = min(max(num_delivered - 1, 0), 63)
    return float(min(config.jetstream_nak_backoff * (2**exponent), config.jetstream_max_backoff))


def _assert_no_overlap(spec: StreamSpec, others: dict[str, list[str]]) -> None:
    """Refuse a declaration that would claim a subject another stream owns.

    The server rejects this too, with err_code 10065, "subjects overlap with an
    existing stream", and no indication of which stream or which subject.
    Catching it here is the difference between a fixable error and a 3am one.
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


#: A listing this long is a broker in a state nobody intended, and looping
#: forever against a server that keeps answering is worse than refusing. The
#: guard reports that it could not check rather than that it checked.
_STREAM_LIST_CEILING = 100_000


async def all_streams(js: Any) -> list[Any]:
    """Every stream on the server, not the first page of them.

    Public because it is the one place that knows how to finish a listing, and
    three callers need it: this module's overlap check, the test tier's broker
    sweep, and whatever changes the termination rule next. Three copies of this
    loop is three chances to stop one page early.

    `_assert_no_overlap` answers "nothing here collides" from whatever it is
    handed, so a truncated listing makes it report a clean check it never
    performed. On a broker holding 406 streams that hid 150 of them, and the
    `BadRequestError` the guard exists to pre-empt escaped raw from
    `add_stream` -- the error its own docstring calls the 3am one.

    WHEN THE LISTING IS DONE IS THE SERVER'S ANSWER, not this module's guess.
    `STREAM.LIST` reports `total`, and `streams_info_iterator` is the nats-py
    call that keeps it -- `streams_info` builds the same list and discards it.
    So there is no page-size constant here: a short page is not read as the
    end, because a server that answered fewer than it could would otherwise
    stop the walk early and hand back a listing this function had promised was
    complete. That is the same defect one layer up from where it was found.

    Iterating the response converts each entry to a `StreamInfo`, which is what
    `streams_info` returns and what every caller reads `.config` off.
    """
    infos: list[Any] = []
    while True:
        page = await js.streams_info_iterator(offset=len(infos))
        found = list(page)
        infos.extend(found)
        if len(infos) >= page.total:
            return infos
        if not found:
            # The server says there are more and will not give them: the one
            # state where returning what we have would be reporting a complete
            # listing we do not have.
            raise StreamDeclarationError(
                f"the broker reported {page.total} streams and returned none at "
                f"offset {len(infos)}, so the listing cannot be finished. Refusing "
                f"to declare a stream against a partial listing: it would report no "
                f"subject overlap without having looked at every stream."
            )
        if len(infos) > _STREAM_LIST_CEILING:
            raise StreamDeclarationError(
                f"the broker reported more than {_STREAM_LIST_CEILING} streams while "
                f"listing them to check for subject overlap. Refusing to declare a "
                f"stream against a listing this module cannot finish reading: a "
                f"partial listing would report no overlap without having looked."
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

    # Before the broker is read: the broker snapshot below is taken once, so a name
    # repeated in this list would meet an absent stream twice, and the second add would
    # be neither checked against the first nor refused for sharing its name.
    names = [spec.name for spec in specs]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise StreamDeclarationError(
            f"stream name(s) {repeated} are declared more than once in one list of "
            f"{len(specs)} streams. One name is one stream: merge the subjects into one "
            f"declaration, or rename one."
        )

    # Also before the broker is read, and for the same reason as the plan below: the client and the
    # server refuse a name, a subject or a duration only when the stream is added, after the
    # streams before it were. Every declaration is checked here, and every refusal is reported.
    refusals = [
        (spec.name, problem)
        for spec in specs
        if (problem := spec.declaration_problem()) is not None
    ]
    if len(refusals) == 1:
        name, problem = refusals[0]
        raise StreamDeclarationError(
            f"stream {name!r} cannot be declared: {problem}. None of the {len(specs)} declared "
            f"streams was created."
        )
    if refusals:
        listed = "\n".join(f"  - stream {name!r}: {problem}" for name, problem in refusals)
        raise StreamDeclarationError(
            f"{len(refusals)} of {len(specs)} declared streams cannot be declared, and none was "
            f"created:\n{listed}"
        )

    infos = await all_streams(js)
    claims: dict[str, list[str]] = {i.config.name: list(i.config.subjects or []) for i in infos}
    server_configs = {i.config.name: i.config for i in infos}
    server_states = {i.config.name: getattr(i, "state", None) for i in infos}

    # First decide, creating nothing: every spec is checked against the broker and against the
    # specs before it, and every conflict is collected. A conflict in a later spec must not leave
    # the earlier ones on a shared broker, holding subject claims for a service that never came up.
    plan: list[tuple[StreamSpec, StreamConfig | None]] = []
    conflicts: list[StreamDeclarationError] = []
    for spec in specs:
        current = server_configs.get(spec.name)
        others = {name: subjects for name, subjects in claims.items() if name != spec.name}
        try:
            if current is None:
                _assert_no_overlap(spec, others)
                plan.append((spec, None))
            elif not spec.matches_declared_fields(current):
                if not allow_update:
                    differing = "; ".join(
                        f"{field}: declared {expected!r}, on the broker {actual!r}"
                        for field, expected, actual in spec.declared_differences(current)
                    )
                    raise StreamDeclarationError(
                        f"stream {spec.name!r} already exists and differs from this service's "
                        f"declaration in {differing}. Refusing to rewrite a stream that another "
                        f"service may own. Set jetstream_update_streams=True to apply the "
                        f"change deliberately, or align the declarations."
                    )
                dropping = spec.limit_below_usage(server_states.get(spec.name))
                if dropping:
                    lowered = "; ".join(
                        f"{field} declared {declared!r}, the stream holds {held!r}"
                        for field, declared, held in dropping
                    )
                    raise StreamDeclarationError(
                        f"stream {spec.name!r} would drop stored messages if updated to this "
                        f"declaration: {lowered}. The server discards what is over a lowered "
                        f"limit, so this is an operator action: make room in the stream, or "
                        f"lower the limit on the broker yourself."
                    )
                _assert_no_overlap(spec, others)
                plan.append((spec, current))
        except StreamDeclarationError as conflict:
            conflicts.append(conflict)
            continue
        claims[spec.name] = list(spec.subjects)

    if len(conflicts) == 1:
        raise conflicts[0]
    if conflicts:
        listed = "\n".join(f"  - {conflict}" for conflict in conflicts)
        raise StreamDeclarationError(
            f"{len(conflicts)} of {len(specs)} declared streams cannot be declared, and none "
            f"was created or changed:\n{listed}"
        )

    # Then apply. The checks above are made against the broker as it was read; a stream created
    # by someone else in between can still fail an add, which this does not make atomic.
    for spec, current in plan:
        if current is None:
            await js.add_stream(config=spec.to_stream_config())
            if logger:
                logger.info(f"Declared JetStream stream {spec.name!r} for {spec.subjects}")
            continue
        differences = spec.declared_differences(current)
        await js.update_stream(config=spec.apply_to(current))
        if logger:
            changes = "; ".join(
                f"{field}: {actual!r} -> {expected!r}" for field, expected, actual in differences
            )
            logger.info(f"Updated JetStream stream {spec.name!r}: {changes}")

    # A replica count is held only when the copies exist: a single server accepts a raised count on
    # an update and keeps one copy. A service must not start believing in replication it lacks.
    for spec in specs:
        if spec.num_replicas is None:
            continue
        shortfall = spec.replica_shortfall(await js.stream_info(spec.name))
        if shortfall is not None:
            raise StreamDeclarationError(
                f"stream {spec.name!r} keeps fewer copies than declared: {shortfall}. This is an "
                f"operator action, not one this service can reconcile: give the stream its "
                f"replicas on a cluster, or lower num_replicas."
            )


async def validate_bound_streams(js: Any, specs: list[StreamSpec]) -> None:
    """Validate named streams without listing, creating, or updating broker resources."""
    for spec in specs:
        try:
            info = await js.stream_info(spec.name)
        except NotFoundError as exc:
            raise StreamDeclarationError(
                f"pre-provisioned stream {spec.name!r} was not found. Create it for "
                f"subjects {sorted(spec.subjects)!r} before starting this service in "
                "JetStream bind mode."
            ) from exc
        shortfall = spec.replica_shortfall(info)
        if shortfall is not None:
            raise StreamDeclarationError(
                f"pre-provisioned stream {spec.name!r} keeps fewer copies than this service "
                f"declares: {shortfall}. Give the stream its replicas, or lower the declaration, "
                "before starting this service in JetStream bind mode."
            )
        if spec.matches_declared_fields(info.config):
            continue
        detail = "; ".join(
            f"{field} is {actual!r}, expected {expected!r}"
            for field, expected, actual in spec.declared_differences(info.config)
        )
        raise StreamDeclarationError(
            f"pre-provisioned stream {spec.name!r} does not match this service's "
            f"declaration: {detail}. Update the provisioned stream or the declaration before "
            "starting this service in JetStream bind mode."
        )

"""JetStream declaration and consumer configuration.

Everything here is pure or takes its JetStream context as an argument, so it
needs no service state and is testable without a broker. The parts that need
service state — the ack policy, the DLQ assertion — live on
``CliffracerService``.
"""

import math
from typing import Any, Literal

from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StorageType, StreamConfig
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

    def matches_declared_fields(self, config: StreamConfig) -> bool:
        """True when every field this declaration owns matches the broker.

        Subject order is not significant. Server-side defaults are compared
        against our own defaults rather than against ``None``, because a
        server round-trip fills fields we left unset.

        Limits, discard policy, replicas, placement, description, metadata and
        fields added by newer brokers are outside ``StreamSpec``. Updates
        preserve those fields from the broker's live configuration.
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
        return differences


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

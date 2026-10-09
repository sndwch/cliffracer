"""Service configuration model validating NATS connection and lifecycle parameters."""

import os
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, ValidationError

from .endpoints import BrokerUrl, redact_nats_url, unusable_host, unusable_nats_url
from .jetstream import StreamSpec
from .subjects import validate_inbox_prefix

_HIDDEN = "<hidden: the input to a ServiceConfig can hold credentials>"
_CREDENTIAL_FIELDS = frozenset({"nats_user", "nats_password", "nats_token"})


def _revealed(secret: SecretStr | str | None) -> str | None:
    """The credential as the connection needs it.

    A `str` is read as it is: `model_copy(update=...)` and `model_construct` skip validation, so a
    config built that way holds the plain string it was given.
    """
    if secret is None or isinstance(secret, str):
        return secret
    return secret.get_secret_value()


def _carries_a_credential(model: type[BaseModel], detail: Any) -> bool:
    """Whether an error's input may hold a credential, and so is not shown."""
    loc = detail["loc"]
    return (
        not loc
        or loc[0] not in model.model_fields
        or loc[0] in _CREDENTIAL_FIELDS
        # A string `nats_url` that is refused is redacted by its own validator. One that is not a
        # string, a YAML list of servers for instance, has no such validator and each entry can
        # carry a password.
        or (loc[0] == "nats_url" and not isinstance(detail["input"], str))
        or isinstance(detail["input"], dict | BaseModel)
    )


class ServiceConfig(BaseModel):
    """Configuration for NATS-based services"""

    name: str
    # A `str` to every caller. What it holds is a `BrokerUrl`, whose repr hides a password embedded
    # in the URL, and a JSON dump carries the URL with that password withheld, as it carries the
    # other credentials. A Python dump keeps the URL whole, so a copy or an overlay still connects.
    nats_url: Annotated[
        str,
        AfterValidator(BrokerUrl),
        PlainSerializer(redact_nats_url, return_type=str, when_used="json"),
    ] = Field(default="nats://localhost:4222", validate_default=True)

    # NATS authentication. All optional; leaving them unset produces exactly
    # the same connect() call as before, so this is backward compatible.
    #
    # Prefer these over embedding credentials in nats_url: a URL-embedded
    # password is easy to leak into logs, argv and error messages.
    nats_user: str | None = Field(default=None)
    nats_password: SecretStr | None = Field(default=None)
    nats_token: SecretStr | None = Field(default=None)
    nats_credentials_file: str | None = Field(default=None)
    nats_inbox_prefix: str | None = Field(
        default=None,
        description="Dedicated request and delivery inbox prefix for this broker role.",
    )

    @model_validator(mode="after")
    def _the_nats_credentials_name_one_way_to_authenticate(self) -> "ServiceConfig":
        """Refuse credentials that name two ways in, or half of one.

        `nats_auth_kwargs()` passes whatever is set and nats-py resolves two methods by its own
        precedence, so which credential was used would be decided by the client library and not by
        the configuration the operator wrote.
        """
        if (self.nats_user is None) != (self.nats_password is None):
            missing = "nats_password" if self.nats_password is None else "nats_user"
            raise ValueError(f"nats_user and nats_password go together: {missing} is not set")
        methods = {
            "nats_user + nats_password": self.nats_user is not None,
            "nats_token": self.nats_token is not None,
            "nats_credentials_file": self.nats_credentials_file is not None,
        }
        named = [name for name, present in methods.items() if present]
        if len(named) > 1:
            raise ValueError(
                f"more than one way to authenticate to NATS is configured ({', '.join(named)}); "
                f"nats-py would pick one by its own precedence. Configure exactly one."
            )
        return self

    @field_validator("nats_url")
    @classmethod
    def _validate_nats_url(cls, value: str) -> str:
        if (problem := unusable_nats_url(value)) is not None:
            # A broker URL may carry a password, and pydantic prints the refused input into the
            # message, `errors()` and the traceback. So the error is built here, with the input
            # redacted, which pydantic accepts as the validator's own failure.
            refusal = InitErrorDetails(
                type="value_error",
                loc=(),
                input=redact_nats_url(value),
                ctx={"error": ValueError(f"nats_url cannot be connected to: {problem}")},
            )
            raise ValidationError.from_exception_data(cls.__name__, [refusal])
        return value

    @field_validator("nats_inbox_prefix")
    @classmethod
    def _validate_inbox_prefix(cls, value: str | None) -> str | None:
        return validate_inbox_prefix(value) if value is not None else None

    # Connection settings
    #
    # Maximum reconnect attempts for established connections (-1 reconnects
    # indefinitely). Connect deadline on initial startup is governed separately
    # by connect_timeout.
    max_reconnect_attempts: int = Field(default=-1, ge=-1)
    # Unchanged deliberately: the budget was the problem, not the pacing.
    reconnect_time_wait: int = Field(default=2, ge=0)

    # How nats-py notices a connection that has stopped answering without resetting the socket:
    # one outstanding ping is counted per `ping_interval`, and the connection is stale when the
    # count passes `max_outstanding_pings`. Unset passes nothing, so nats-py's defaults (120 s, 2)
    # stay.
    ping_interval: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Seconds between the pings that check a quiet connection is still answering. "
            "Unset leaves nats-py's default of 120. A silent partition is noticed between "
            "`max_outstanding_pings * ping_interval` and `(max_outstanding_pings + 1) * "
            "ping_interval` seconds after it begins."
        ),
    )
    max_outstanding_pings: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Pings that may go unanswered before the connection is treated as lost and "
            "reconnection starts. Unset leaves nats-py's default of 2. At least 1: zero would "
            "call a healthy connection lost at its first tick."
        ),
    )

    # The readiness check asks the broker for a round trip, because nats-py's own connection flag
    # stays True for minutes after a connection goes silent without being reset. The result is
    # reused for `broker_probe_cache` seconds, and callers that arrive meanwhile share one probe.
    broker_probe_timeout: float | None = Field(
        default=2.0,
        gt=0,
        description=(
            "Seconds the readiness check waits for the broker to answer a round trip. A round "
            "trip that fails or takes longer makes the status `disconnected`. `None` turns the "
            "probe off, and readiness reads nats-py's connection flag alone."
        ),
    )
    broker_probe_cache: float = Field(
        default=1.0,
        ge=0,
        description=(
            "Seconds the readiness check reuses the last round trip's result, a failure "
            "included, so a burst of probes costs one round trip. `0` probes on every check."
        ),
    )

    # A listener declared with `pause_when_down` stops consuming while one of the dependencies it
    # names is down. Those dependencies, and only those, are probed in the background on this
    # interval; `/ready` and `/health` keep probing every dependency per request.
    dependency_probe_interval: float = Field(
        default=5.0,
        gt=0,
        allow_inf_nan=False,
        description=(
            "Seconds between background probes of the dependencies a listener names in "
            "`pause_when_down`. No background probe runs when no listener names one."
        ),
    )
    dependency_pause_after: int = Field(
        default=2,
        ge=1,
        description=(
            "Consecutive failed background probes after which a dependency counts as down and "
            "the listeners that name it in `pause_when_down` stop consuming."
        ),
    )
    dependency_resume_after: int = Field(
        default=2,
        ge=1,
        description=(
            "Consecutive passing background probes after which a dependency counts as up again "
            "and a listener paused on it resumes, once every dependency it names is up."
        ),
    )

    # Maximum seconds the initial connect attempt may take before raising
    # NatsError and initiating shutdown. None disables the timeout.
    connect_timeout: float | None = Field(default=30.0, gt=0)

    # Seconds to drain active tasks before cancellation, followed by an equal cancellation grace.
    shutdown_timeout: float | None = Field(
        default=30.0,
        gt=0,
        description=(
            "Seconds a timer's run in flight is given to finish (all timers at once, "
            "first), then seconds to drain active tasks, followed by an equal "
            "cancellation grace, and then the connection's drain, bounded by the same number "
            "of seconds. `None` waits without a deadline. A value at or below zero, which "
            "this field refuses, can reach a stop only around validation; there it is bounded "
            "at 30 seconds and logged as a warning. A stop begun because "
            "the broker connection closed for good is cut off after 10 seconds whatever "
            "this is set to; its `on_shutdown` still runs after the cut-off, for up to "
            "this many seconds (30 when this is `None`)."
        ),
    )

    # Whether to stop the service when the broker connection is permanently closed
    # (a graceful `stop()`, which also stops the health listener). The process is not
    # terminated with a special exit code by this setting. If False, the service stays
    # up and reports status 'disconnected'.
    exit_on_closed: bool = Field(default=True)

    # Request settings
    request_timeout: float = Field(default=30.0, gt=0)
    serialization_format: Literal["json", "msgpack"] = Field(default="json")
    expose_internal_errors: bool = Field(
        default=False,
        description=(
            "Whether an exception's own text may leave the process. Governs wire RPC "
            "error responses (with a traceback), the reply to a `{service}.describe` that "
            "fails, the reason a caller is given when an extension that fails closed "
            "raises from its own check, and the health endpoint's error strings — a "
            "failing dependency probe, an extension's `health_details`, a failure in the "
            "extension loop itself, the dependency sweep and the catch-all 500 — and the "
            "`error` of a distributed cron firing's record in its KV bucket, and the "
            "`error` of the dead letter of a handler that failed on its last delivery. Unset, "
            "each reports a fixed string and the detail stays in the log (the cron record "
            "and the dead letter hold the exception's type); the status, the "
            "`ok` flags, the failing dependency names and the name of the extension that "
            "refused are reported either way. A refusal a check authored itself — "
            "`refused: unauthenticated` — is not an exception's text and is delivered "
            "whole regardless."
        ),
    )
    rpc_validation_errors: Literal["full", "redacted"] = Field(
        default="full",
        description=(
            "RPC request validation diagnostics: full preserves Pydantic details; redacted "
            "uses fixed diagnostics in replies, async logs, and validation exception chains. "
            "Does not redact raw payloads, event DLQs, or handler/response errors."
        ),
    )
    max_rpc_concurrency: int | None = Field(
        default=None,
        gt=0,
        description="Maximum concurrent RPC handlers executing simultaneously.",
    )
    max_event_concurrency: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Maximum concurrent event handlers executing simultaneously, on push and pull "
            "listeners alike. A pull consumer's fetched batch is dispatched concurrently "
            "and this is what bounds it; `None` leaves the batch size and "
            "`jetstream_max_ack_pending` as the only bounds. A JetStream message that waits "
            "for a permit is sent in-progress pulses while it waits, so the server does not "
            "redeliver it; a service that is stopping starts none of them, and the broker "
            "redelivers them."
        ),
    )
    max_async_rpc_concurrency: int | None = Field(
        default=None,
        gt=0,
        description="Maximum concurrent async fire-and-forget RPC handlers executing simultaneously.",
    )
    max_rpc_in_flight: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Most RPC requests admitted at once on each of the request-reply and "
            "fire-and-forget paths: running, or waiting for a concurrency permit. A request "
            "over it is answered with code `busy` (a fire-and-forget one is dropped and logged) "
            "and not started. Unset: no bound; before the delivery callback, nats-py's "
            "per-subscription pending limit (524288 messages or 128 MiB by default) still "
            "applies. See Admission and the wait for a permit in the API reference."
        ),
    )
    max_stream_items: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Items a handler that streams its reply may send in one stream. The item that would "
            "pass it is not sent: the stream ends with code `refused`, naming the limit and the "
            "items sent. None sets no limit."
        ),
    )
    max_stream_bytes: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Bytes of item bodies a handler that streams its reply may send in one stream. The "
            "item that would pass it is not sent: the stream ends with code `refused`, naming the "
            "limit and the items sent. None sets no limit."
        ),
    )
    max_rpc_processing_time: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Seconds an RPC handler may run, including its wait for a concurrency permit, "
            "before it is cancelled. A request-reply handler is also bounded by the budget its "
            "caller sends in `Cliffracer-Timeout-Ms`, whichever ends first, and is answered "
            "with code `deadline_exceeded`; a fire-and-forget handler is bounded by this alone "
            "and logged. None sets no bound of the service's own. See Request headers and "
            "deadlines in the API reference."
        ),
    )

    # JetStream settings. With jetstream_enabled False, which is the default,
    # the service itself subscribes, publishes and dispatches on core NATS and
    # none of the fields below changes that -- the default is the
    # backward-compatibility guarantee for services running on core NATS.
    jetstream_enabled: bool = Field(default=False)

    jetstream_resource_mode: Literal["provision", "bind"] = Field(
        default="provision",
        description=(
            "`'provision'` lists streams and creates missing declared streams and durable "
            "consumers. `'bind'` reads each declared stream and consumer by name, "
            "validates its contract, and binds without resource creation or account-wide "
            "LIST/NAMES access."
        ),
    )

    # Streams this service declares at startup. Coverage is checked against
    # THIS list, not against what happens to exist on the server: startup that
    # depends on what else is running is startup that works in production and
    # fails in a fresh environment.
    jetstream_streams: list[StreamSpec] = Field(default_factory=list)

    # Consumer tuning, shared by every durable listener on the service.
    #
    # WRITE-ONCE PER DURABLE. A durable consumer's configuration is fixed when
    # the consumer is first created. A later subscribe does not update it:
    # nats-py adopts the existing consumer's config wholesale, so changing any
    # of these three and restarting leaves the service asking for one thing and
    # the server doing another, with nothing raised. Cliffracer logs a warning
    # naming the drifted fields at subscribe time. For `jetstream_max_deliver`
    # the dead-letter decision uses the lower of the server's value and this
    # one, so a message the server stops redelivering is still dead-lettered.
    # To actually apply a change, delete the consumer and let it be recreated:
    #
    #     nats consumer rm <STREAM> <durable>
    #
    # Existing server consumer parameters are fixed at creation; consumer recreation
    # is required to apply changes.
    jetstream_max_deliver: int = Field(default=5, ge=1)
    jetstream_ack_wait: float = Field(default=30.0, gt=0)
    jetstream_max_ack_pending: int = Field(default=64, ge=1)

    # Nak backoff: base seconds, doubled per delivery, capped.
    # Batch size for pull consumer message retrieval per cycle.
    jetstream_pull_batch: int = Field(default=8, ge=1)
    jetstream_pull_timeout: float = Field(default=5.0, gt=0)

    # How long ONE replica may hold a JetStream message before the framework
    # stops vouching for it. `jetstream_ack_wait` is not that bound: the
    # heartbeat resets the server's ack timer for as long as the handler has
    # not returned, so a wedged handler -- a deadlocked lock, a call with no
    # timeout, an await on an event nothing sets -- is vouched for forever and
    # `jetstream_max_deliver` and the dead-letter queue never fire.
    #
    # None means unbounded, which is the behaviour every existing deployment
    # has. Set it and a handler that outlives it is CANCELLED and the message
    # naked, then dead-lettered like any other failure.
    max_processing_time: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Seconds one replica may spend on a JetStream message before the handler "
            "is cancelled and the message redelivered. None leaves it unbounded, which "
            "is what the heartbeat does today."
        ),
    )

    jetstream_nak_backoff: float = Field(default=1.0, ge=0)
    jetstream_max_backoff: float = Field(default=60.0, ge=0)

    # Off by default. Two services declaring the same stream name with
    # different subject sets would otherwise rewrite it on every boot and flap
    # a stream carrying live traffic. Evolving a stream is a deliberate act.
    jetstream_update_streams: bool = Field(default=False)

    @model_validator(mode="after")
    def _bound_resources_cannot_be_updated(self) -> "ServiceConfig":
        if self.jetstream_resource_mode == "bind" and self.jetstream_update_streams:
            raise ValueError(
                "jetstream_update_streams cannot be enabled when "
                "jetstream_resource_mode='bind'; update pre-provisioned resources outside "
                "the service, then restart it"
            )
        return self

    # Automatic idempotency key generation on publish_event
    idempotent_publishing: bool = Field(
        default=False,
        description="Whether to automatically generate idempotency keys from domain payloads.",
    )

    # Lifecycle hooks
    on_connect: Callable[[], Any] | None = None
    on_disconnect: Callable[[], Any] | None = None
    on_error: Callable[[Exception], Any] | None = None

    # Service metadata
    version: str = Field(default="0.1.0")

    # Built-in HTTP health probe listener configuration. health_host defaults
    # to loopback (127.0.0.1) for local container probes; set to "0.0.0.0"
    # to allow external probing.
    health_listener: bool = Field(default=True)
    health_host: str = Field(default="127.0.0.1")
    health_port: int = Field(default=8000, ge=0, le=65535)

    @field_validator("health_host")
    @classmethod
    def _validate_health_host(cls, value: str) -> str:
        # The empty string is how asyncio spells "every interface", so it binds and is allowed.
        if value and (problem := unusable_host(value)) is not None:
            raise ValueError(f"health_host cannot be bound: {problem}")
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v:
            raise ValueError("service name must not be empty")
        if any(c.isspace() for c in v):
            raise ValueError(f"service name must not contain whitespace: {v!r}")
        if any(c in v for c in "*>"):
            raise ValueError(f"service name must not contain wildcards ('*', '>'): {v!r}")
        if any(token == "" for token in v.split(".")):
            raise ValueError(
                f"service name must not have empty tokens (leading, trailing, or doubled '.'): {v!r}"
            )
        return v

    description: str | None = None

    # Namespace for multi-app disambiguation (optional; single subject token)
    namespace: str | None = Field(default=None)

    @field_validator("namespace")
    @classmethod
    def _validate_namespace(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v or any(c in v for c in ".*>") or any(c.isspace() for c in v):
            raise ValueError(
                f"namespace must be a single subject token (no '.', '*', '>', or whitespace): {v!r}"
            )
        return v

    # An environment prefix, outside the namespace, for every name this service
    # puts on a broker: subjects, stream names, stream subjects and durable
    # consumers. Two deployments of the same services can then share one broker
    # without meeting each other -- staging beside production, a tenant beside
    # another tenant, or two test runs beside each other.
    #
    # It sits OUTSIDE `namespace` deliberately. A `cross_namespace=True`
    # listener subscribes to `*.<pattern>`, and `*` matches exactly one token,
    # so a prefix inside the wildcard would be read across environments by the
    # very feature that exists to read across namespaces. Outside it, the same
    # listener resolves to `<prefix>.*.<pattern>` and stays within its own.
    subject_prefix: str | None = Field(
        default_factory=lambda: os.environ.get("CLIFFRACER_SUBJECT_PREFIX") or None,
        validate_default=True,
        description=(
            "Outermost prefix for every subject, stream and durable consumer this "
            "service touches, separating one environment's or tenant's traffic from "
            "another's on a shared broker. Applied outside `namespace`, so a "
            "`cross_namespace` listener resolves to `<subject_prefix>.*.<pattern>` "
            "and still reads only its own environment. Letters, digits and "
            "underscores only: it also names streams and durables, which take no "
            "`.`. Defaults from `$CLIFFRACER_SUBJECT_PREFIX`."
        ),
    )

    @field_validator("subject_prefix")
    @classmethod
    def _validate_subject_prefix(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # Stricter than a subject token, because the same prefix has to render
        # into a stream name and a durable consumer name, and neither of those
        # accepts a dot.
        if not v or not all(c.isalnum() or c == "_" for c in v):
            # The field validates its default (pydantic does not unless asked), so a prefix that
            # came from the environment is refused here too; say which variable it was.
            source = (
                " (read from $CLIFFRACER_SUBJECT_PREFIX)"
                if v == os.environ.get("CLIFFRACER_SUBJECT_PREFIX")
                else ""
            )
            raise ValueError(
                "subject_prefix must be one token of letters, digits or underscores "
                f"(it also names streams and durables, which take no '.'): {v!r}{source}"
            )
        return v

    @property
    def effective_jetstream_streams(self) -> list[StreamSpec]:
        """The declared streams as they exist on the broker, under the prefix.

        A view rather than a rewrite of `jetstream_streams`. Prefixing at
        construction would double-apply on any round trip -- `model_validate` of
        a dumped config revalidates it while the dump already carries the
        prefixed names, and `validate_assignment=True` means a later assignment
        to any field revalidates the model too. Computing it here keeps the
        declaration the author wrote and makes the prefix a property of how the
        declaration is read.
        """
        prefix = self.subject_prefix
        if not prefix:
            return self.jetstream_streams
        return [
            spec.model_copy(
                update={
                    "name": f"{prefix}_{spec.name}",
                    "subjects": [f"{prefix}.{subject}" for subject in spec.subjects],
                }
            )
            for spec in self.jetstream_streams
        ]

    def prefixed_name(self, name: str) -> str:
        """Render a non-subject name -- a stream or a durable -- under the prefix."""
        return f"{self.subject_prefix}_{name}" if self.subject_prefix else name

    # Auto-restart configuration
    auto_restart: bool = Field(default=True)
    restart_delay: float = Field(default=1.0, ge=0)

    # Message validation / invalid-message handling
    default_on_invalid: Literal["deadletter", "drop"] = Field(default="deadletter")
    dlq_subject: str = Field(default="dlq.{service}")

    @model_validator(mode="after")
    def _the_dlq_template_must_render_a_usable_subject(self) -> "ServiceConfig":
        """Render the template now, and refuse it if the result is not a subject.

        `dlq_subject` is the only subject on this config that is a template
        rather than a subject, so it is the only one whose validity depends on
        the other fields. `{namespace}` with no namespace set renders an empty
        token -- `.dlq.orders` -- which NATS refuses.

        Checked here rather than at publish time because this subject is used
        only when a message is already being dead-lettered: the first message to
        exercise a wrong template is one that was going to be dropped anyway,
        now failing to be recorded, and the broker's complaint (`nats: invalid
        subject`) names neither the template nor the field that produced it.

        Rendered by the declared builder rather than here, so what is validated
        is exactly the subject that will be published to -- prefix included --
        and there is no second copy of the formatting to drift. The subject-builder
        guard caught the first draft of this check rendering the template itself,
        and said to build it through a helper instead.
        """
        from .decorators import _unusable_subject_reason
        from .discovery import HandlerDiscovery

        try:
            rendered = HandlerDiscovery.dlq_subject(self)
        except (KeyError, IndexError) as exc:
            raise ValueError(
                f"dlq_subject names a placeholder this config cannot fill: "
                f"{self.dlq_subject!r} wants {exc.args[0]!r}, and only {{service}} "
                f"and {{namespace}} are available."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - any other way a template fails to render
            raise ValueError(
                f"dlq_subject cannot be rendered: {self.dlq_subject!r} raises "
                f"{type(exc).__name__}: {exc}. Only {{service}} and {{namespace}} are available, "
                f"without attribute or index access."
            ) from exc

        reason = _unusable_subject_reason(rendered)
        if reason is not None:
            detail = ""
            if "{namespace}" in self.dlq_subject and not self.namespace:
                detail = (
                    " The template names {namespace} and this config has none, so that "
                    "token renders empty. Either set `namespace` or drop {namespace} "
                    "from the template."
                )
            raise ValueError(
                f"dlq_subject renders an unusable subject: {self.dlq_subject!r} "
                f"becomes {rendered!r}, and {reason}.{detail}"
            )
        return self

    def nats_auth_kwargs(self) -> dict:
        """The auth keyword arguments to pass to ``nats.connect()``.

        Only credentials that were actually configured are included, so a
        config with none of them produces an identical call to one made before
        auth existed.

        This is a method rather than inline logic at each call site on purpose:
        there is more than one place that opens a NATS connection (the service
        itself, and the optimised connection pool), and duplicating the
        decision is how the pool came to silently ignore credentials in the
        first place.
        """
        mapping = {
            "user": self.nats_user,
            "password": _revealed(self.nats_password),
            "token": _revealed(self.nats_token),
            "user_credentials": self.nats_credentials_file,
        }
        return {k: v for k, v in mapping.items() if v is not None}

    def nats_connect_kwargs(self) -> dict:
        """The keyword arguments to ``nats.connect()`` that say who the connection is.

        The credentials from ``nats_auth_kwargs`` and, when one is configured, the
        ``inbox_prefix``: the subject prefix a connection's replies arrive on, which a broker
        permission can confine a user to. Both the service and the connection pool take their
        connection from here, so a pooled request is answered where the service's own would be.
        """
        kwargs = self.nats_auth_kwargs()
        if self.nats_inbox_prefix is not None:
            kwargs["inbox_prefix"] = self.nats_inbox_prefix
        return kwargs

    @model_validator(mode="wrap")
    @classmethod
    def _no_refusal_carries_the_input(cls, data: Any, handler: Any) -> Any:
        """Re-raise a refusal with the input hidden where it can hold a credential.

        pydantic attaches the input to every error, and for a refusal by a model validator, a
        missing field or a misspelled name that input is the whole mapping the caller wrote, or
        the model being assigned to: the broker password and token, and a password embedded in
        `nats_url`. `str(error)` prints a head and tail of it, and `errors()` and `json()` carry
        all of it. A value that belongs to one field is kept, so the message still says what was
        wrong with it; the credential fields and anything that is not one field's value are not.

        Declared after every other model validator, which makes it the outermost: a validator
        declared later runs around the ones before it, so it sees their refusals too.
        """
        try:
            return handler(data)
        except ValidationError as error:
            details = [
                InitErrorDetails(
                    type=detail["type"],
                    loc=detail["loc"],
                    input=_HIDDEN if _carries_a_credential(cls, detail) else detail["input"],
                    ctx=detail.get("ctx", {}),
                )
                for detail in error.errors(include_url=False)
            ]
            raise ValidationError.from_exception_data(cls.__name__, details) from None

    # extra="forbid" ensures unknown or misspelled configuration options
    # fail validation immediately at startup.
    model_config = ConfigDict(
        arbitrary_types_allowed=True, extra="forbid", validate_assignment=True
    )

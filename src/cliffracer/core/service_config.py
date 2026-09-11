"""Service configuration model validating NATS connection and lifecycle parameters."""

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .jetstream import StreamSpec


class ServiceConfig(BaseModel):
    """Configuration for NATS-based services"""

    name: str
    nats_url: str = Field(default="nats://localhost:4222")

    # NATS authentication. All optional; leaving them unset produces exactly
    # the same connect() call as before, so this is backward compatible.
    #
    # Prefer these over embedding credentials in nats_url: a URL-embedded
    # password is easy to leak into logs, argv and error messages.
    nats_user: str | None = Field(default=None)
    nats_password: str | None = Field(default=None)
    nats_token: str | None = Field(default=None)
    nats_credentials_file: str | None = Field(default=None)

    # Connection settings
    #
    # Maximum reconnect attempts for established connections (-1 reconnects
    # indefinitely). Connect deadline on initial startup is governed separately
    # by connect_timeout.
    max_reconnect_attempts: int = Field(default=-1, ge=-1)
    # Unchanged deliberately: the budget was the problem, not the pacing.
    reconnect_time_wait: int = Field(default=2, ge=0)

    # Maximum seconds the initial connect attempt may take before raising
    # NatsError and initiating shutdown. None disables the timeout.
    connect_timeout: float | None = Field(default=30.0, gt=0)

    # Maximum seconds allowed to drain active tasks during shutdown before cancellation.
    shutdown_timeout: float | None = Field(
        default=30.0,
        gt=0,
        description="Maximum seconds allowed to drain active tasks during shutdown before cancellation.",
    )

    # Whether to terminate the process with exit code 75 when the broker
    # connection is permanently closed. If False, the service remains alive
    # and reports status 'disconnected'.
    exit_on_closed: bool = Field(default=True)

    # Request settings
    request_timeout: float = Field(default=30.0, gt=0)
    serialization_format: Literal["json", "msgpack"] = Field(default="json")
    expose_internal_errors: bool = Field(
        default=False,
        description="Whether to include full exception tracebacks in wire RPC error responses.",
    )
    max_rpc_concurrency: int | None = Field(
        default=None,
        gt=0,
        description="Maximum concurrent RPC handlers executing simultaneously.",
    )
    max_event_concurrency: int | None = Field(
        default=None,
        gt=0,
        description="Maximum concurrent event handlers executing simultaneously.",
    )
    max_async_rpc_concurrency: int | None = Field(
        default=None,
        gt=0,
        description="Maximum concurrent async fire-and-forget RPC handlers executing simultaneously.",
    )

    # JetStream settings. Every field below is ignored unless jetstream_enabled
    # is True, which it is not by default — that default is the entire
    # backward-compatibility guarantee for services running on core NATS.
    jetstream_enabled: bool = Field(default=False)

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
    # naming the drifted fields at subscribe time; to actually apply a change,
    # delete the consumer and let it be recreated:
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

    jetstream_nak_backoff: float = Field(default=1.0, ge=0)
    jetstream_max_backoff: float = Field(default=60.0, ge=0)

    # Off by default. Two services declaring the same stream name with
    # different subject sets would otherwise rewrite it on every boot and flap
    # a stream carrying live traffic. Evolving a stream is a deliberate act.
    jetstream_update_streams: bool = Field(default=False)

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

    # Logging configuration
    log_level: str = Field(default="INFO")

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

    # Auto-restart configuration
    auto_restart: bool = Field(default=True)
    restart_delay: float = Field(default=1.0, ge=0)

    # Message validation / invalid-message handling
    default_on_invalid: Literal["deadletter", "drop"] = Field(default="deadletter")
    dlq_subject: str = Field(default="dlq.{service}")

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
            "password": self.nats_password,
            "token": self.nats_token,
            "user_credentials": self.nats_credentials_file,
        }
        return {k: v for k, v in mapping.items() if v is not None}

    # extra="forbid" ensures unknown or misspelled configuration options
    # fail validation immediately at startup.
    model_config = ConfigDict(
        arbitrary_types_allowed=True, extra="forbid", validate_assignment=True
    )

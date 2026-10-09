"""Immutable contracts and addresses for locally supervised service activations."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from cliffracer.client import ServiceClient
from cliffracer.core.connection import BrokerConnectionState
from cliffracer.core.exceptions import CliffracerError
from cliffracer.core.outputs import OutputBindings
from cliffracer.core.service_config import ServiceConfig
from cliffracer.introspect import Description, canonical


class SupervisionError(CliffracerError):
    """A service template or activation operation cannot be accepted."""


class TemplateError(SupervisionError):
    """A template declaration or construction result violates its contract."""


class ActivationConflict(SupervisionError):
    """An identity is already associated with a different accepted request."""


class ActivationCapacityError(SupervisionError):
    """A finite supervisor limit prevents admission."""


class ActivationUnavailable(SupervisionError):
    """An activation reference is unknown, expired or unavailable."""


class ContractMismatch(TemplateError):
    """The complete RPC method set or its signatures differ."""

    def __init__(
        self, *, changed: tuple[str, ...], missing: tuple[str, ...], extra: tuple[str, ...]
    ):
        self.changed = changed
        self.missing = missing
        self.extra = extra
        super().__init__(
            f"RPC contract mismatch: changed={changed}, missing={missing}, extra={extra}"
        )


@dataclass(frozen=True)
class RpcContract:
    """Canonical method signatures, including model-schema hashes in their type references."""

    signatures: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted((name, signature) for name, signature in self.signatures))
        if len(dict(ordered)) != len(ordered) or any(not n or not s for n, s in ordered):
            raise TemplateError("RPC method names and signatures must be nonempty and unique")
        object.__setattr__(self, "signatures", ordered)

    @classmethod
    def from_description(cls, description: Description) -> RpcContract:
        return cls(tuple((m.name, m.signature_hash) for m in description.methods))

    @property
    def identity(self) -> str:
        return "sha256:" + hashlib.sha256(canonical(dict(self.signatures)).encode()).hexdigest()

    def verify(self, description: Description) -> None:
        """Check a live description against the complete registered method set."""
        self.verify_signatures(dict(self.from_description(description).signatures))

    def verify_signatures(self, signatures: Mapping[str, str]) -> None:
        expected = dict(self.signatures)
        changed = tuple(
            sorted(n for n in expected.keys() & signatures.keys() if expected[n] != signatures[n])
        )
        missing = tuple(sorted(expected.keys() - signatures.keys()))
        extra = tuple(sorted(signatures.keys() - expected.keys()))
        if changed or missing or extra:
            raise ContractMismatch(changed=changed, missing=missing, extra=extra)


@dataclass(frozen=True)
class LogicalIdentity:
    scope: str
    template: str
    key: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.scope, self.template, self.key)
        ):
            raise ValueError("scope, template and key must be nonempty strings")


@dataclass(frozen=True)
class ActivationAddress:
    """Service routing components; broker credentials belong to the caller."""

    service: str
    namespace: str | None = None
    subject_prefix: str | None = None

    def __post_init__(self) -> None:
        ServiceConfig(
            name=self.service, namespace=self.namespace, subject_prefix=self.subject_prefix
        )


@dataclass(frozen=True)
class ActivationReference:
    """A generation-pinned value, not an ownership or authorization capability."""

    identity: LogicalIdentity
    incarnation: str
    generation: int
    revision: str
    contract: RpcContract
    address: ActivationAddress
    outputs: OutputBindings = field(default_factory=OutputBindings)

    def __post_init__(self) -> None:
        if not self.incarnation or not self.revision:
            raise ValueError("incarnation and revision must be nonempty")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation must be a positive integer")

    def bind[C: ServiceClient](self, client_type: type[C], **options: Any) -> C:
        """Construct an ordinary generated client at exactly this activation address."""
        if not issubclass(client_type, ServiceClient):
            raise TemplateError("activation clients must extend ServiceClient")
        if {"service", "namespace", "subject_prefix"} & options.keys():
            raise TemplateError("an activation client's routing comes from its reference")
        self.contract.verify_signatures(client_type.SIGNATURES)
        return client_type(
            service=self.address.service,
            namespace=self.address.namespace or "",
            subject_prefix=self.address.subject_prefix or "",
            **options,
        )


class ActivationState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    UNFINISHED = "unfinished"


@dataclass(frozen=True)
class CleanupOutcome:
    complete: bool
    unfinished_tasks: int = 0

    def __post_init__(self) -> None:
        if self.unfinished_tasks < 0 or (self.complete and self.unfinished_tasks):
            raise ValueError("completed cleanup has no unfinished tasks")


@dataclass(frozen=True)
class ActivationSnapshot:
    """Lifecycle observations without application settings or broker credentials."""

    reference: ActivationReference
    owner: str
    state: ActivationState
    reason: str | None = None
    cleanup: CleanupOutcome | None = None
    broker_state: BrokerConnectionState | None = None
    retry_until: datetime | None = None


__all__ = [
    "ActivationAddress",
    "ActivationCapacityError",
    "ActivationConflict",
    "ActivationReference",
    "ActivationSnapshot",
    "ActivationState",
    "ActivationUnavailable",
    "CleanupOutcome",
    "ContractMismatch",
    "LogicalIdentity",
    "RpcContract",
    "SupervisionError",
    "TemplateError",
]

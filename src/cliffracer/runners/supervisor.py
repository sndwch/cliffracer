"""Explicit, bounded ownership of service activations on one event loop."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from pydantic import BaseModel

from cliffracer.core.connection import BrokerConnectionState
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.loop_host import abandon
from cliffracer.core.outputs import OutputProducer
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.introspect import Description

from .contracts import (
    ActivationAddress,
    ActivationCapacityError,
    ActivationConflict,
    ActivationReference,
    ActivationSnapshot,
    ActivationState,
    ActivationUnavailable,
    CleanupOutcome,
    LogicalIdentity,
)
from .templates import (
    NormalizedSettings,
    RegisteredTemplate,
    ServiceTemplate,
    TemplateCatalog,
    _copy_runtime_config,
)

_TERMINAL = {ActivationState.STOPPED, ActivationState.FAILED}

# A service's stop gives each of up to four sequential phases (a timer run's grace, the drain of
# active tasks, the cancellation grace, the connection's drain) its `shutdown_timeout`. A child's
# phases get a fifth of the cleanup budget each: the four sum to 80% of it, and the rest is room for
# the work between them, so the supervisor reads a child that has closed.
_CHILD_STOP_PHASES = 5


@dataclass(frozen=True)
class SupervisorLimits:
    max_active: int = 64
    max_records: int = 256
    max_owners: int = 64
    max_depth: int = 8
    max_page: int = 100
    startup_timeout: float = 30.0
    cleanup_timeout: float = 30.0
    wait_timeout: float = 60.0
    retention: float = 300.0
    monitor_interval: float = 0.05

    def __post_init__(self) -> None:
        for value in (
            self.max_active,
            self.max_records,
            self.max_owners,
            self.max_depth,
            self.max_page,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("supervisor capacity limits must be positive integers")
        for budget in (
            self.startup_timeout,
            self.cleanup_timeout,
            self.wait_timeout,
            self.retention,
            self.monitor_interval,
        ):
            if not math.isfinite(budget) or budget <= 0:
                raise ValueError("supervisor time budgets must be finite and positive")


@dataclass(frozen=True)
class OwnerHandle:
    """A supervisor-issued lifetime; applications control who receives the handle."""

    incarnation: str
    identifier: str
    scope: str


@dataclass(frozen=True)
class ActivationPage:
    items: tuple[ActivationSnapshot, ...]
    cursor: str | None


@dataclass(frozen=True)
class CleanupReport:
    activations: tuple[ActivationSnapshot, ...]

    @property
    def complete(self) -> bool:
        return all(item.cleanup is not None and item.cleanup.complete for item in self.activations)


class ActivationTerminated(ActivationUnavailable):
    """A retained activation outcome, including cleanup, replaces a ready reference."""

    def __init__(self, snapshot: ActivationSnapshot):
        self.snapshot = snapshot
        super().__init__(f"activation is {snapshot.state}: {snapshot.reason}")


@dataclass
class _Owner:
    handle: OwnerHandle
    parent: str | None
    depth: int
    closed: bool = False
    expires: float | None = None
    close_task: asyncio.Task[CleanupReport] | None = None


@dataclass
class _Activation:
    reference: ActivationReference
    template: RegisteredTemplate[Any] = field(repr=False)
    settings: NormalizedSettings[Any] = field(repr=False)
    owner: str
    sequence: int
    state: ActivationState = ActivationState.STARTING
    reason: str | None = None
    failed: bool = False
    cleanup_error: bool = False
    child: CliffracerService | None = field(default=None, repr=False)
    operation: asyncio.Task[None] | None = field(default=None, repr=False)
    startup: asyncio.Task[None] | None = field(default=None, repr=False)
    shutdown: asyncio.Task[None] | None = field(default=None, repr=False)
    cleanup_task: asyncio.Task[None] | None = field(default=None, repr=False)
    available: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    cleanup: CleanupOutcome | None = None
    expires: float | None = None
    retry_until: datetime | None = None
    successor: str | None = None


class LocalSupervisor:
    """Own child lifecycles without taking ownership of process signals or the loop.

    Admission and state changes run without awaits on the owning event loop.
    Accepted startup and cleanup run in supervisor-owned tasks. Caller timeouts
    and cancellation affect only the caller's wait.
    """

    def __init__(
        self,
        runtime: ServiceConfig,
        *,
        limits: SupervisorLimits | None = None,
        catalog: TemplateCatalog | None = None,
    ) -> None:
        self._runtime = _copy_runtime_config(runtime)
        self.limits = limits or SupervisorLimits()
        self.catalog = catalog or TemplateCatalog()
        self.incarnation = uuid.uuid4().hex
        self._owners: dict[str, _Owner] = {}
        self._records: dict[str, _Activation] = {}
        self._current: dict[LogicalIdentity, _Activation] = {}
        self._sequence = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[CleanupReport] | None = None
        self._closed = False

    @property
    def _log(self) -> Any:
        """The loguru logger bound to the host service this supervisor runs in."""
        return logger.bind(service=self._runtime.name)

    def register[S: BaseModel](self, template: ServiceTemplate[S]) -> RegisteredTemplate[S]:
        return self.catalog.register(template)

    def __deepcopy__(self, memo: dict[int, Any]) -> LocalSupervisor:
        raise TypeError("a supervisor cannot be cloned; use SharedDependency for shared ownership")

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._closed:
            raise ActivationUnavailable("supervisor is closed")
        if self._loop is not None and self._loop is not loop:
            raise ActivationUnavailable("supervisor belongs to another event loop")
        if self._loop is None:
            self._loop = loop
            self._monitor_task = asyncio.create_task(self._monitor(), name="service_supervisor")

    async def __aenter__(self) -> LocalSupervisor:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _clock(self) -> float:
        if self._loop is None or asyncio.get_running_loop() is not self._loop:
            raise ActivationUnavailable("start the supervisor on this event loop first")
        return self._loop.time()

    def _owner(self, handle: OwnerHandle, *, open_required: bool = False) -> _Owner:
        self._clock()
        owner = self._owners.get(handle.identifier)
        if owner is None or owner.handle != handle:
            raise ActivationUnavailable("owner is unknown or expired")
        if open_required and (self._closed or owner.closed):
            raise ActivationUnavailable("owner admission is closed")
        return owner

    async def open_owner(self, scope: str, *, parent: OwnerHandle | None = None) -> OwnerHandle:
        """Open a caller-owned lifetime, optionally nested under another owner."""
        self._prune()
        if self._closed:
            raise ActivationUnavailable("supervisor admission is closed")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("owner scope must be nonempty")
        ancestor = self._owner(parent, open_required=True) if parent is not None else None
        depth = ancestor.depth + 1 if ancestor is not None else 1
        if len(self._owners) >= self.limits.max_owners or depth > self.limits.max_depth:
            raise ActivationCapacityError("owner capacity or nesting depth is exhausted")
        handle = OwnerHandle(self.incarnation, uuid.uuid4().hex, scope)
        self._owners[handle.identifier] = _Owner(
            handle, ancestor.handle.identifier if ancestor is not None else None, depth
        )
        return handle

    async def supervisor_owner(self, scope: str) -> OwnerHandle:
        """Explicitly choose a lifetime that the host closes with the supervisor."""
        return await self.open_owner(scope)

    def _capacity(self) -> None:
        self._prune()
        if len(self._records) >= self.limits.max_records:
            raise ActivationCapacityError("retained activation capacity is exhausted")
        if sum(r.state not in _TERMINAL for r in self._records.values()) >= self.limits.max_active:
            raise ActivationCapacityError("active service capacity is exhausted")

    def _admit(
        self,
        owner: _Owner,
        identity: LogicalIdentity,
        template: RegisteredTemplate[Any],
        settings: NormalizedSettings[Any],
        generation: int,
    ) -> _Activation:
        outputs = template.bind_outputs(settings, self._runtime)
        self._capacity()
        name = "activation_" + self.incarnation + "_" + uuid.uuid4().hex
        outputs = replace(
            outputs,
            producer=OutputProducer(
                identity.scope,
                identity.template,
                identity.key,
                template.definition.revision,
                self.incarnation,
                generation,
                name,
            ),
        )
        reference = ActivationReference(
            identity,
            self.incarnation,
            generation,
            template.definition.revision,
            template.contract,
            ActivationAddress(name, self._runtime.namespace, self._runtime.subject_prefix),
            outputs,
        )
        self._sequence += 1
        record = _Activation(reference, template, settings, owner.handle.identifier, self._sequence)
        self._records[name] = self._current[identity] = record
        record.operation = asyncio.create_task(self._activate(record), name="service_activation")
        return record

    async def ensure(
        self,
        owner: OwnerHandle,
        template: str,
        key: str,
        settings: BaseModel | Mapping[str, Any],
        *,
        revision: str,
    ) -> ActivationReference:
        """Return a matching ready generation or its retained terminal outcome."""
        self._prune()
        lifetime = self._owner(owner, open_required=True)
        registered = self.catalog.resolve(template, revision)
        identity = LogicalIdentity(owner.scope, template, key)
        record = self._current.get(identity)
        if record is not None and (
            record.owner != owner.identifier or record.reference.revision != revision
        ):
            raise ActivationConflict("identity belongs to another owner or revision")
        normalized = registered.normalize(
            settings, defaults=record.settings if record is not None else None
        )
        if record is None:
            record = self._admit(lifetime, identity, registered, normalized, 1)
        elif (
            record.owner != owner.identifier
            or record.reference.revision != revision
            or record.settings != normalized
        ):
            raise ActivationConflict("identity belongs to another owner, revision or settings")
        return await self._wait_ready(record)

    async def _wait_ready(self, record: _Activation) -> ActivationReference:
        async def observe() -> ActivationReference:
            while record.state in {ActivationState.STARTING, ActivationState.STOPPING}:
                await record.available.wait()
            if record.state == ActivationState.READY:
                return record.reference
            raise ActivationTerminated(self._snapshot(record))

        try:
            return await asyncio.wait_for(observe(), timeout=self.limits.wait_timeout)
        except TimeoutError:
            raise ActivationUnavailable("caller wait expired; activation remains owned") from None

    def _reference(self, reference: ActivationReference, *, current: bool = False) -> _Activation:
        self._prune()
        record = self._records.get(reference.address.service)
        if record is None or record.reference != reference:
            raise ActivationUnavailable("activation reference is unknown or expired")
        if current and self._current.get(reference.identity) is not record:
            raise ActivationUnavailable("activation reference is superseded")
        return record

    async def reactivate(
        self, owner: OwnerHandle, terminal: ActivationReference
    ) -> ActivationReference:
        """Accept one successor for a retained terminal generation; retries share it."""
        lifetime = self._owner(owner, open_required=True)
        previous = self._reference(terminal)
        if previous.owner != owner.identifier:
            raise ActivationConflict("activation belongs to another owner")
        if previous.successor is not None:
            successor = self._records.get(previous.successor)
            if successor is None:
                raise ActivationUnavailable("reactivation retry guarantee expired")
        else:
            if previous.state not in _TERMINAL:
                raise ActivationConflict("reactivation requires verified terminal cleanup")
            if self._current.get(terminal.identity) is not previous:
                raise ActivationUnavailable("activation reference is superseded")
            successor = self._admit(
                lifetime,
                terminal.identity,
                previous.template,
                previous.settings,
                terminal.generation + 1,
            )
            previous.successor = successor.reference.address.service
        return await self._wait_ready(successor)

    async def _start_and_verify(self, record: _Activation, child: CliffracerService) -> None:
        await child.start()
        if child.nc is None:
            raise ActivationUnavailable("child has no broker connection")
        subject = HandlerDiscovery.with_namespace(child.config, f"{child.config.name}.describe")
        response = await child.nc.request(subject, b"", timeout=self.limits.startup_timeout)
        description = Description.from_dict(json.loads(response.data))
        record.template.contract.verify(description)
        record.template.output_contract.verify(description.outputs)

    @staticmethod
    def _retrieve(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    async def _activate(self, record: _Activation) -> None:
        if record.state != ActivationState.STARTING:
            return
        try:
            data = self._runtime.model_dump(mode="python", round_trip=True)
            cleanup_budget = min(
                self.limits.cleanup_timeout, record.template.definition.cleanup_timeout
            )
            data.update(
                name=record.reference.address.service,
                health_port=0,
                shutdown_timeout=cleanup_budget / _CHILD_STOP_PHASES,
            )
            record.child = record.template.construct(
                record.settings,
                ServiceConfig.model_validate(data),
                bindings=record.reference.outputs,
            )
            record.startup = asyncio.create_task(
                self._start_and_verify(record, record.child), name="service_startup"
            )
            record.startup.add_done_callback(self._retrieve)
            done, _ = await asyncio.wait(
                [record.startup],
                timeout=min(
                    self.limits.startup_timeout, record.template.definition.startup_timeout
                ),
            )
            if not done:
                self._request_stop(record, failure="startup deadline expired")
            elif record.startup.cancelled():
                self._request_stop(record, failure="startup cancelled")
            elif (startup_error := record.startup.exception()) is not None:
                self._request_stop(
                    record,
                    failure="startup or contract verification failed",
                    error=startup_error,
                )
            elif record.state == ActivationState.STARTING:
                if self._closed or self._owners[record.owner].closed:
                    self._request_stop(record)
                else:
                    record.state = ActivationState.READY
                    record.available.set()
        except asyncio.CancelledError:
            self._request_stop(record, failure="activation interrupted")
            raise
        except Exception as exc:
            self._request_stop(record, failure="construction or startup failed", error=exc)

    def _request_stop(
        self,
        record: _Activation,
        *,
        failure: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        if record.state in _TERMINAL or record.cleanup_task is not None:
            return
        if failure is not None:
            # The activation and the exception's type, and no message text: inspection excludes
            # raw exception messages because they can carry application settings and credentials.
            cause = f" ({type(error).__name__})" if error is not None else ""
            self._log.warning(
                f"Activation {record.reference.address.service} failed: {failure}{cause}"
            )
        record.failed = failure is not None
        record.reason = failure or "stop requested"
        record.state = ActivationState.STOPPING
        record.available.clear()
        record.cleanup_task = asyncio.create_task(self._cleanup(record), name="service_cleanup")

    def _pending(self, record: _Activation) -> set[asyncio.Task[Any]]:
        tasks = {t for t in (record.startup, record.shutdown) if t is not None and not t.done()}
        if record.child is not None:
            tasks.update(t for t in record.child.container.lifecycle.active_tasks if not t.done())
            tasks.update(t for t in record.child.container.connection.subscriptions if not t.done())
        return tasks

    def _resources_closed(self, record: _Activation) -> bool:
        if record.shutdown is not None and record.shutdown.done():
            if record.shutdown.cancelled() or record.shutdown.exception() is not None:
                if not record.cleanup_error:
                    cause = (
                        "cancelled"
                        if record.shutdown.cancelled()
                        else type(record.shutdown.exception()).__name__
                    )
                    self._log.error(
                        f"Activation {record.reference.address.service} lifecycle cleanup "
                        f"failed ({cause})"
                    )
                record.cleanup_error = True
                record.reason = "lifecycle cleanup failed"
        if self._pending(record) or record.cleanup_error:
            return False
        if record.child is None:
            return True
        child = record.child
        connection = child.container.connection
        return (
            not child.container.lifecycle.is_running
            and not child.container.lifecycle.is_starting
            and child.health_listener.port is None
            and (child.nc is None or child.nc.is_closed)
            and not connection._subscription_handles
        )

    def _outcome(self, record: _Activation) -> None:
        if self._resources_closed(record):
            record.state = ActivationState.FAILED if record.failed else ActivationState.STOPPED
            record.cleanup = CleanupOutcome(True)
            record.expires = self._clock() + self.limits.retention
            record.retry_until = datetime.now(UTC) + timedelta(seconds=self.limits.retention)
        else:
            record.state = ActivationState.UNFINISHED
            pending = self._pending(record)
            self._log.error(
                f"Activation {record.reference.address.service} cleanup did not finish: "
                f"{len(pending)} task(s) still running are abandoned and the activation keeps "
                f"its capacity"
            )
            record.cleanup = CleanupOutcome(False, len(pending))
            for task in pending:
                abandon(task)
            for control_task in (record.operation, record.cleanup_task):
                if control_task is not None and not control_task.done():
                    abandon(control_task)
        record.available.set()

    async def _cleanup(self, record: _Activation) -> None:
        try:
            if record.child is not None:
                record.shutdown = asyncio.create_task(record.child.stop(), name="service_shutdown")
                record.shutdown.add_done_callback(self._retrieve)
                # A failed stop is recorded and logged by `_outcome`, through `_resources_closed`.
                await asyncio.wait(
                    [record.shutdown],
                    timeout=min(
                        self.limits.cleanup_timeout, record.template.definition.cleanup_timeout
                    ),
                )
        finally:
            self._outcome(record)

    async def stop(self, reference: ActivationReference) -> CleanupOutcome:
        record = self._reference(reference, current=True)
        self._request_stop(record)
        if record.cleanup_task is not None:
            await asyncio.shield(record.cleanup_task)
        assert record.cleanup is not None
        return record.cleanup

    def _descendants(self, owner: str) -> set[str]:
        descendants = {owner}
        for _ in range(self.limits.max_depth):
            children = {key for key, item in self._owners.items() if item.parent in descendants}
            if children <= descendants:
                break
            descendants.update(children)
        return descendants

    async def close_owner(self, handle: OwnerHandle) -> CleanupReport:
        owner = self._owner(handle)
        if owner.close_task is None:
            descendants = self._descendants(handle.identifier)
            for key in descendants:
                self._owners[key].closed = True
            records = tuple(r for r in self._records.values() if r.owner in descendants)
            for record in records:
                self._request_stop(record)
            owner.close_task = asyncio.create_task(
                self._close_records(records, descendants), name="service_owner_cleanup"
            )
        return await asyncio.shield(owner.close_task)

    async def close(self) -> CleanupReport:
        self._clock()
        if self._close_task is None:
            self._closed = True
            for owner in self._owners.values():
                owner.closed = True
            records = tuple(self._records.values())
            for record in records:
                self._request_stop(record)
            self._close_task = asyncio.create_task(
                self._close_records(records, set(self._owners)), name="service_supervisor_cleanup"
            )
        return await asyncio.shield(self._close_task)

    async def _close_records(
        self, records: tuple[_Activation, ...], owners: set[str]
    ) -> CleanupReport:
        tasks = {
            r.cleanup_task
            for r in records
            if r.cleanup_task is not None and not r.cleanup_task.done()
        }
        if tasks:
            await asyncio.wait(tasks, timeout=self.limits.cleanup_timeout)
        for record in records:
            if record.state == ActivationState.STOPPING:
                self._outcome(record)
        for key in owners:
            owner = self._owners.get(key)
            if owner is not None:
                owner.expires = self._clock() + self.limits.retention
        if self._closed and self._monitor_task is not None:
            if all(r.state in _TERMINAL for r in self._records.values()):
                self._monitor_task.cancel()
                await asyncio.gather(self._monitor_task, return_exceptions=True)
            else:
                abandon(self._monitor_task)
        return CleanupReport(tuple(self._snapshot(r) for r in records))

    @property
    def unfinished_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Retain actual unfinished work for hosts that manage their own event loop."""
        tasks = {task for record in self._records.values() for task in self._pending(record)}
        tasks.update(
            task
            for record in self._records.values()
            if record.state == ActivationState.UNFINISHED
            for task in (record.operation, record.cleanup_task)
            if task is not None and not task.done()
        )
        return tuple(tasks)

    async def _monitor(self) -> None:
        while True:
            for record in tuple(self._records.values()):
                child = record.child
                if record.state == ActivationState.READY and child is not None:
                    if not child.container.is_running:
                        self._request_stop(record, failure="child lifecycle terminated")
                    elif child.broker_state == BrokerConnectionState.CLOSED:
                        # The lifecycle is still running, as it is under `exit_on_closed=False`:
                        # the cause is the connection, and saying the lifecycle ended is not true.
                        self._request_stop(record, failure="child broker connection closed")
                elif record.state == ActivationState.UNFINISHED and self._resources_closed(record):
                    self._outcome(record)
            self._prune()
            if self._closed and all(r.state in _TERMINAL for r in self._records.values()):
                return
            await asyncio.sleep(self.limits.monitor_interval)

    def _prune(self) -> None:
        now = self._clock()
        for name, record in tuple(self._records.items()):
            if record.expires is not None and record.expires <= now and record.state in _TERMINAL:
                del self._records[name]
                if self._current.get(record.reference.identity) is record:
                    del self._current[record.reference.identity]
        for key, owner in tuple(self._owners.items()):
            if owner.expires is not None and owner.expires <= now:
                if owner.close_task is not None and not owner.close_task.done():
                    continue
                descendants = self._descendants(key)
                if not any(r.owner in descendants for r in self._records.values()):
                    del self._owners[key]

    def _snapshot(self, record: _Activation) -> ActivationSnapshot:
        cleanup = record.cleanup
        if record.state == ActivationState.UNFINISHED:
            cleanup = CleanupOutcome(False, len(self._pending(record)))
        return ActivationSnapshot(
            record.reference,
            record.owner,
            record.state,
            record.reason,
            cleanup,
            record.child.broker_state if record.child is not None else None,
            record.retry_until,
        )

    async def inspect(self, identity: LogicalIdentity) -> ActivationSnapshot | None:
        self._prune()
        record = self._current.get(identity)
        return self._snapshot(record) if record is not None else None

    async def list_activations(
        self, scope: str, *, cursor: str | None = None, limit: int = 50
    ) -> ActivationPage:
        """Page by admission sequence, excluding admissions after the first page."""
        self._prune()
        if type(limit) is not int or not 1 <= limit <= self.limits.max_page:
            raise ValueError("page limit is outside the supervisor's finite bounds")
        after, ceiling = 0, self._sequence
        if cursor is not None:
            try:
                incarnation, scoped, after, ceiling = json.loads(base64.urlsafe_b64decode(cursor))
                if (
                    incarnation != self.incarnation
                    or scoped != scope
                    or type(after) is not int
                    or type(ceiling) is not int
                    or not 0 <= after <= ceiling <= self._sequence
                ):
                    raise ValueError
            except (ValueError, TypeError):
                raise ActivationUnavailable("invalid activation page cursor") from None
        candidates = sorted(
            (
                r
                for r in self._records.values()
                if r.reference.identity.scope == scope and after < r.sequence <= ceiling
            ),
            key=lambda r: r.sequence,
        )
        selected = candidates[:limit]
        next_cursor = None
        if len(candidates) > limit:
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([self.incarnation, scope, selected[-1].sequence, ceiling]).encode()
            ).decode()
        return ActivationPage(tuple(self._snapshot(r) for r in selected), next_cursor)


__all__ = [
    "ActivationPage",
    "ActivationTerminated",
    "CleanupReport",
    "LocalSupervisor",
    "OwnerHandle",
    "SupervisorLimits",
]

"""Distributed leader-elected cron scheduling using NATS JetStream Key-Value store.

Guarantees that across multiple horizontally scaled service replicas, exactly one
replica executes the scheduled cron job per interval, with active lease locking
to prevent overlapping executions of long-running tasks.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import nats.js.errors

from cliffracer.core.clock import Clock
from cliffracer.core.error_text import may_expose
from cliffracer.core.exceptions import ConfigurationError

from .cron import CronTimer

# Valid NATS KV key regex: ^[-/_=\.a-zA-Z0-9]+$ (no colons allowed)
_KV_KEY_SAFE_RE = re.compile(r"[^-_=\.a-zA-Z0-9]")


#: The JetStream API error a compare-and-delete or compare-and-put answers with when the key has
#: moved on since the revision given.
_WRONG_LAST_SEQUENCE = 10071


# How long a write that has been given up on is given to unwind before it is left.
_TO_UNWIND = 1.0

# How long a write that finishes a firing may take before it is given up on: twice the five seconds
# nats-py allows a request to a stream, so a write that would have failed by itself is never cut short.
_FINISH_TIMEOUT = 10.0


class _UntilDone:
    """Runs the writes a firing must finish to their end, whatever cancels the task meanwhile.

    A firing that has taken the interval record owes two writes before it is gone: the outcome in
    that record and the release of the `.active` lease. A cancellation that lands while one is in
    flight, the service stopping or a timer's grace running out, raises at that `await` and skips
    the rest, which leaves the lease and a `running` record for `lease_ttl`: every replica then
    skips every firing as "prior run still active". `run` lets the write finish, as a task of its
    own, and notes that the task was cancelled; the caller raises the cancellation once all of its
    writes are done. The task stays alive meanwhile, so whoever waits for it (`Timer.stop`, the
    service's drain) waits for the cleanup too.
    """

    def __init__(self, bound: float) -> None:
        self.cancelled = False
        self._bound = bound

    async def _wait(self, task: asyncio.Future[Any], timeout: float) -> None:
        try:
            await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            self.cancelled = True

    async def run(self, awaitable: Awaitable[Any]) -> Any:
        """The write's result, or `TimeoutError` when it has not returned within the bound.

        A write that never returns, a broker that went away in the middle of the finish, must not
        hold the task, and so the stop that waits for it, open for ever: it is cancelled after the
        bound and given a moment to unwind, and its failure is logged by the caller as any failed
        write is. What it would have written is left to the bucket's TTL, as before.
        """
        task = asyncio.ensure_future(awaitable)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._bound
        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                task.cancel()
                await self._wait(task, _TO_UNWIND)
                raise TimeoutError(f"the write did not return within {self._bound:g}s")
            await self._wait(task, remaining)
        return task.result()


def _replica_id(service: Any) -> str:
    """What the records call this replica: the service's `instance_id` if it sets one, else `<hostname>-<pid>`.

    In a container the hostname is the pod or container name, so the id in a record, in the skip
    warning and in a log line is the same replica every time it fires.
    """
    named = getattr(service, "instance_id", None) or getattr(service, "_instance_id", None)
    return named or f"{socket.gethostname()}-{os.getpid()}"


def _sanitize_key_part(part: str) -> str:
    """Sanitize key part to conform to NATS KV valid key regex (no colons)."""
    return _KV_KEY_SAFE_RE.sub("_", part)


def _job_key_prefix(namespace: str | None, service: str, method: str) -> str:
    """The key stem of one job's records: `cron.<namespace>.<service>.<method>`.

    The namespace is what separates two apps that share a broker, a bucket and a service name, so
    it is part of every key built from this stem (the active lease, the per-interval lock and the
    eager lock). A service with no namespace has `cron.<service>.<method>`.
    """
    parts = ["cron", *([namespace] if namespace else []), service, method]
    return ".".join(_sanitize_key_part(part) for part in parts)


def _check_the_options(bucket: Any, lease_ttl: Any, no_overlap: Any) -> None:
    """Refuse an option the timer cannot run with, when it is built, as `Timer` does for its own.

    A `lease_ttl` of zero or less made `no_overlap` skip nothing (a lease is honoured while its age
    is below it), `nan` and a string failed at every firing, and a bucket name the KV layer refuses
    came back from `start()` as a warning and failed at the first firing.
    """
    if (
        isinstance(lease_ttl, bool)
        or not isinstance(lease_ttl, int | float)
        or not math.isfinite(lease_ttl)
        or lease_ttl <= 0
    ):
        raise ConfigurationError(
            f"@cron lease_ttl must be a finite number of seconds above zero, got {lease_ttl!r} "
            f"({type(lease_ttl).__name__})"
        )
    if not isinstance(no_overlap, bool):
        raise ConfigurationError(
            f"@cron no_overlap must be True or False, got {no_overlap!r} ({type(no_overlap).__name__})"
        )
    if not isinstance(bucket, str):
        raise ConfigurationError(
            f"@cron bucket must be a bucket name, got {bucket!r} ({type(bucket).__name__})"
        )
    try:
        from cliffracer_kv import BucketConfig, BucketConfigError
    except ImportError:  # the distributed cron cannot start without it, and says so then
        return
    try:
        BucketConfig(name=bucket)
    except BucketConfigError as exc:
        raise ConfigurationError(f"@cron bucket {bucket!r} cannot be used: {exc}") from exc


class DistributedCronTimer(CronTimer):
    """A CronTimer that coordinates across cluster replicas using cliffracer-kv.

    `eager` runs the job once per cluster for as long as the bucket keeps the `.eager` key, and not
    on every start: the first start takes the key and leaves it, so a restart or a rolling deploy
    inside the bucket's TTL does not run it again, on this replica or any other. The TTL is
    `max(lease_ttl, 300)` seconds for a bucket the timer creates, whatever an existing bucket has,
    and a bucket with no TTL never gives the key up, so the job never runs eagerly again. Work that
    must run after every start belongs in the service's `on_startup`.
    """

    def __init__(
        self,
        expression: str,
        tz: str = "UTC",
        eager: bool = False,
        max_drift: float = 1.0,
        error_backoff: float = 5.0,
        headers: dict[str, str] | None = None,
        token_factory: Callable[[], str | Awaitable[str]] | None = None,
        *,
        distributed: bool = True,
        bucket: str = "cron_locks",
        lease_ttl: float = 300.0,
        no_overlap: bool = True,
        kv_extension: Any = None,
        clock: Clock | None = None,
        deadline: float | None = None,
    ) -> None:
        super().__init__(
            expression=expression,
            tz=tz,
            eager=eager,
            max_drift=max_drift,
            error_backoff=error_backoff,
            headers=headers,
            token_factory=token_factory,
            clock=clock,
            deadline=deadline,
        )
        _check_the_options(bucket, lease_ttl, no_overlap)
        if no_overlap and deadline is not None and deadline > lease_ttl:
            raise ConfigurationError(
                f"@cron deadline={deadline:g}s is longer than lease_ttl={lease_ttl:g}s: a firing "
                f"still running when its lease ends is overlapped by another replica's, which "
                f"no_overlap is there to prevent. Give the deadline at most the lease_ttl."
            )
        self.distributed = distributed
        self.bucket = bucket
        self.lease_ttl = lease_ttl
        self.no_overlap = no_overlap
        self._finish_timeout = _FINISH_TIMEOUT
        self._explicit_kv = kv_extension

    @property
    def finish_timeout(self) -> float:
        """Seconds a write that finishes a firing may take before it is given up on.

        The outcome written to the interval record and the release of the lease each run under it
        once the handler has returned, so a broker that goes away mid-finish cannot hold a stop open
        for ever: the write is cancelled and logged, and the bucket's TTL removes what it would have
        written. Ten seconds by default, twice the five nats-py allows a JetStream request.
        """
        return self._finish_timeout

    @finish_timeout.setter
    def finish_timeout(self, seconds: float) -> None:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int | float)
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise ValueError(
                f"finish_timeout must be a finite number of seconds above zero, got {seconds!r}"
            )
        self._finish_timeout = float(seconds)

    def clone(self) -> DistributedCronTimer:
        """Create an independent copy of this DistributedCronTimer."""
        c = DistributedCronTimer(
            expression=self.expression,
            tz=self.tz,
            eager=self.eager,
            max_drift=self.max_drift,
            error_backoff=self.error_backoff,
            headers=dict(self.headers) if self.headers else None,
            token_factory=self.token_factory,
            distributed=self.distributed,
            bucket=self.bucket,
            lease_ttl=self.lease_ttl,
            no_overlap=self.no_overlap,
            kv_extension=self._explicit_kv,
            clock=self.clock,
            deadline=self.deadline,
        )
        c.method_name = self.method_name
        c.finish_timeout = self.finish_timeout
        return c

    @property
    def _schedule_description(self) -> str:
        return f"distributed cron '{self.expression}' [{self.tz}] in bucket '{self.bucket}'"

    def _find_kv_extension(self, service: Any, extensions: Any = None) -> Any:
        """Resolve the KvExtension the timer keeps its locks in.

        The explicit `kv_extension=` handle wins. Otherwise it is the service's
        `KvExtension`, whatever attribute name it was declared under. A service
        that declares several uses the one named `kv`, and refuses to guess when
        none is. Something that is not a `KvExtension`, named `kv` and able to serve
        a bucket (`get_bucket`), as a stand-in is, is accepted last, so a double needs no handle.
        """
        if self._explicit_kv is not None:
            return self._explicit_kv

        if extensions is None:
            extensions = getattr(getattr(service, "container", None), "extensions", None)
        declared = list(extensions or [])

        try:
            from cliffracer_kv import KvExtension
        except ImportError:
            KvExtension = None  # type: ignore[assignment,misc]  # cliffracer-kv is optional

        if KvExtension is not None:
            found = [ext for ext in declared if isinstance(ext, KvExtension)]
            if len(found) > 1:
                named_kv = [ext for ext in found if getattr(ext, "name", None) == "kv"]
                if len(named_kv) == 1:
                    return named_kv[0]
                names = ", ".join(repr(getattr(ext, "name", "?")) for ext in found)
                raise ConfigurationError(
                    f"Handler '{self.method_name}' declares @cron(distributed=True), but the "
                    f"service declares {len(found)} KvExtensions ({names}) and none is named "
                    f"'kv', so it cannot tell which holds the cron locks. Name one of them `kv`."
                )
            if found:
                return found[0]

        # A name is not a type: only something that can serve a bucket is taken for the store, so
        # an unrelated extension that happens to be called `kv` is not.
        candidate = getattr(service, "kv", None)
        if callable(getattr(candidate, "get_bucket", None)):
            return candidate
        for ext in declared:
            if getattr(ext, "name", None) == "kv" and callable(getattr(ext, "get_bucket", None)):
                return ext

        return None

    def check_declared_dependencies(self, service: Any, handler_name: str, extensions: Any) -> None:
        """Refuse, before the service starts, a distributed timer with no KvExtension to use."""
        if self._find_kv_extension(service, extensions) is None:
            service_name = getattr(getattr(service, "config", None), "name", None)
            raise ConfigurationError(
                f"Handler '{handler_name}' on service '{service_name}' declares "
                f"@cron(distributed=True), but no KvExtension is registered on the service. "
                f"Distributed cron requires cliffracer-kv to be bound to the service."
            )

    async def _get_raw_bucket(self) -> Any:
        """Retrieve the raw nats.js.kv.KeyValue handle, configuring TTL if possible."""
        kv_ext = self._find_kv_extension(self.service_instance)
        if kv_ext is None:
            raise ConfigurationError(
                f"Handler '{self.method_name}' declares @cron(distributed=True), "
                f"but no KvExtension is registered on service. "
                f"Distributed cron requires cliffracer-kv to be bound to the service."
            )

        from cliffracer_kv import BucketConfig

        return await kv_ext.get_bucket(
            self.bucket,
            default_config=BucketConfig(name=self.bucket, ttl=max(self.lease_ttl, 300.0)),
        )

    async def start(self, service_instance: Any) -> None:
        """Start the distributed timer, verifying KvExtension presence.

        A timer that is already running is left to the base class, which warns
        and returns: the KvExtension is checked for a start that would begin a
        loop, not for one that does nothing. A KvExtension that goes away under
        a running timer surfaces in the loop, where the bucket lookup raises.
        """
        if self.is_running:
            await super().start(service_instance)
            return

        kv = self._find_kv_extension(service_instance)
        if kv is None:
            svc_name = (
                getattr(getattr(service_instance, "config", None), "name", None)
                or type(service_instance).__name__
            )
            raise ConfigurationError(
                f"Handler '{self.method_name}' on service '{svc_name}' declares "
                f"@cron(distributed=True), but no KvExtension is registered on the service. "
                f"Distributed cron requires cliffracer-kv to be bound to the service."
            )

        # Set before the check: the bucket is found through the service, and the base class sets it
        # only once it starts the loop.
        self.service_instance = service_instance
        await self._open_the_bucket_and_refuse_a_lease_it_cannot_hold()
        await super().start(service_instance)

    async def _open_the_bucket_and_refuse_a_lease_it_cannot_hold(self) -> None:
        """Open the bucket, and refuse to start a job whose `lease_ttl` it cannot honour.

        The bucket is opened here for every job, so a bucket that cannot be created is reported at
        start. Only a job with `no_overlap` holds a lease, so only it is checked against the TTL: a
        job without it writes no lease, and the interval records it does write live the bucket's
        TTL whatever its `lease_ttl` says.

        The bucket's TTL is fixed by whichever job opened it first (a bucket that is already open,
        or already on the broker, is used as it is), and every key in it, active leases included,
        expires after it. A job that asked for a longer lease than that would have its overlap
        lease vanish early without a word. A bucket that keeps keys longer than asked loses
        nothing, and one with no expiry (a TTL of 0 or none) holds any lease.

        A bucket whose TTL cannot be read is not a reason to refuse: the loop reads it again, and
        reports what it cannot do there.
        """
        try:
            bucket = await self._get_raw_bucket()
            ttl = (await bucket.status()).ttl
        except Exception as e:
            unchecked = (
                f", so its lease_ttl of {self.lease_ttl:g}s is unchecked" if self.no_overlap else ""
            )
            self._log.warning(
                f"Distributed cron '{self.method_name}': could not read the TTL of bucket "
                f"{self.bucket!r}{unchecked}: {e}"
            )
            return
        if self.no_overlap and isinstance(ttl, int | float) and 0 < ttl < self.lease_ttl:
            raise ConfigurationError(
                f"Handler '{self.method_name}' declares lease_ttl={self.lease_ttl:g}s, but bucket "
                f"{self.bucket!r} expires its keys after {ttl:g}s, so its lease would vanish early. "
                f"The bucket's TTL is set by whichever job opened it first: give this job a "
                f"bucket of its own, or make the first job's lease_ttl at least {self.lease_ttl:g}s."
            )

    def _lease_started_at(self, record: dict[str, Any], key: str) -> float | None:
        """When the running lease says it started, or `None` after saying why it cannot be read.

        A lease whose age cannot be read is an unreadable lease, and gets the policy every other
        unreadable lease gets: it is reported and the interval runs. Reading a missing
        `started_at` as 0 made the lease infinitely old, so it was silently run over; reading
        `Infinity` or a date far ahead made it infinitely fresh, so it skipped every interval until
        the bucket's own expiry removed it. A writer in this package always records a finite
        number, so anything else is a record someone else wrote or one that was damaged; one
        `lease_ttl` ahead of now is the most a holder's clock can honestly be.
        """
        value = record.get("started_at")
        if value is None:
            reason = "it has none"
        elif isinstance(value, bool) or not isinstance(value, int | float | str):
            reason = f"{value!r} is not a number"
        else:
            try:
                started_at = float(value)
            except ValueError:
                reason = f"{value!r} is not a number"
            else:
                if not math.isfinite(started_at):
                    reason = f"{value!r} is not finite"
                elif started_at - time.time() > self.lease_ttl:
                    reason = f"{value!r} is more than a lease ({self.lease_ttl:g}s) in the future"
                else:
                    return started_at
        self._log.warning(
            f"Distributed cron '{self.method_name}': the active lease {key} has no readable "
            f"started_at ({reason}), so its age is unknown; treating it as unreadable and running."
        )
        return None

    async def _execute_distributed(self, target_time: datetime, eager: bool = False) -> None:
        """Attempt to atomically acquire interval lock and execute the job."""
        raw_bucket = await self._get_raw_bucket()
        service_config = getattr(self.service_instance, "config", None)
        service_name = getattr(service_config, "name", "cliffracer")
        method_name = self.method_name or "unknown"
        stem = _job_key_prefix(
            getattr(service_config, "namespace", None), service_name, method_name
        )

        epoch_str = "eager" if eager else str(int(target_time.timestamp()))

        active_key = f"{stem}.active"
        interval_key = f"{stem}.{epoch_str}"

        # 1. Overlap check via active lease
        if self.no_overlap:
            try:
                active_entry = await raw_bucket.get(active_key)
                if active_entry and getattr(active_entry, "value", None):
                    active_data = json.loads(active_entry.value.decode("utf-8"))
                    if active_data.get("status") == "running":
                        started_at = self._lease_started_at(active_data, active_key)
                        if started_at is not None and (time.time() - started_at) < self.lease_ttl:
                            self._log.warning(
                                f"Distributed cron '{method_name}' skipped: prior run still active on "
                                f"replica '{active_data.get('replica')}'"
                            )
                            return
            except (
                nats.js.errors.KeyNotFoundError,
                nats.js.errors.NotFoundError,
                nats.js.errors.KeyDeletedError,
            ):
                pass
            except Exception as e:
                self._log.warning(f"Error checking active lease {active_key}: {e}")

        # 2. Atomic interval lock acquisition
        instance_id = _replica_id(self.service_instance)
        payload = {
            "service": service_name,
            "method": method_name,
            "replica": instance_id,
            "status": "running",
            "target_time": target_time.isoformat(),
            "started_at": time.time(),
        }
        payload_bytes = json.dumps(payload).encode("utf-8")

        # A write that is under way is finished, so that whether this replica holds the record is
        # known before it can be cancelled: a cancellation that landed in the middle of it left a
        # `running` record for a run that never happened, or none, and nothing said which.
        cleanup = _UntilDone(self.finish_timeout)
        try:
            rev = await cleanup.run(raw_bucket.create(interval_key, payload_bytes))
        except nats.js.errors.KeyWrongLastSequenceError:
            self._log.info(
                f"Distributed cron '{method_name}' for epoch {epoch_str} already acquired by peer replica. Skipping."
            )
            if cleanup.cancelled:
                raise asyncio.CancelledError from None
            return
        except Exception:
            if cleanup.cancelled:
                raise asyncio.CancelledError from None
            raise

        lease_revision: int | None = None
        exec_error: str | None = None
        exec_error_type: str | None = None
        cancelled = False
        t0 = time.time()
        try:
            # 3. We won the lock! Set active lease if no_overlap. The revision the write returns is
            # what step 6 releases it by, so a run only ever removes the lease it wrote. Inside the
            # `try` so that a cancellation from here on is recorded and the lease released.
            if self.no_overlap and not cleanup.cancelled:
                try:
                    lease_revision = await cleanup.run(raw_bucket.put(active_key, payload_bytes))
                except Exception as e:
                    self._log.warning(f"Failed to record active lease for {method_name}: {e}")
            if cleanup.cancelled:
                raise asyncio.CancelledError

            # 4. Execute the method
            # `_execute_method` handles a handler's failure itself, so the failure
            # is read from the timer rather than from an exception.
            await self._execute_method()
            exec_error = self.last_error
            exec_error_type = self.last_error_type
        except asyncio.CancelledError:
            # The service stopped this run. It is neither the handler's failure nor a refusal, and
            # it did not complete: `CancelledError` is not an `Exception`, so neither branch
            # below saw it and the record said `completed`. The cancellation is raised once the
            # record is written.
            cancelled = True
            raise
        except Exception as exc:
            exec_error = f"{type(exc).__name__}: {exc}"
            exec_error_type = type(exc).__name__
            raise
        finally:
            # 5. Update interval record with completion status and duration.
            # Do NOT delete the interval record: it must persist so trailing replicas do not re-execute!
            duration_ms = (time.time() - t0) * 1000.0
            refusal = self.last_refusal
            if cancelled:
                payload["status"] = "cancelled"
            else:
                payload["status"] = (
                    "failed" if exec_error else "refused" if refusal else "completed"
                )
            payload["completed_at"] = time.time()
            payload["duration_ms"] = duration_ms
            if exec_error:
                # The record is readable by whoever can read the bucket, so it holds the
                # exception's text only where `expose_internal_errors` lets that text leave the
                # process, and its type otherwise.
                config = getattr(self.service_instance, "config", None)
                payload["error"] = (
                    exec_error if may_expose(config) else (exec_error_type or "Exception")
                )
            elif refusal and not cancelled:
                payload["refusal"] = refusal

            try:
                await cleanup.run(
                    raw_bucket.update(interval_key, json.dumps(payload).encode("utf-8"), last=rev)
                )
            except Exception as e:
                self._log.warning(f"Failed to update cron completion record {interval_key}: {e}")

            # 6. Clear the active lease, but only the one this run wrote. A run that outlives
            # `lease_ttl` is run over by a later one, whose lease this run must leave in place:
            # deleting it would let a third run start beside the second. A lease whose write
            # failed (no revision) is not ours to delete either.
            if self.no_overlap and lease_revision:
                try:
                    await cleanup.run(raw_bucket.delete(active_key, last=lease_revision))
                except nats.js.errors.BadRequestError as e:
                    if isinstance(e, nats.js.errors.KeyWrongLastSequenceError) or (
                        getattr(e, "err_code", None) == _WRONG_LAST_SEQUENCE
                    ):
                        self._log.info(
                            f"Active lease {active_key} now belongs to a later run; leaving it."
                        )
                    else:
                        self._log.warning(f"Failed to clear active lease {active_key}: {e}")
                except Exception as e:
                    self._log.warning(f"Failed to clear active lease {active_key}: {e}")

            if cleanup.cancelled:
                # Cancelled while the writes above were under way: they are done, and so is the
                # cancellation.
                raise asyncio.CancelledError

    async def _timer_loop(self) -> None:
        """Main distributed cron execution loop."""
        if self.eager:
            # Handled as a scheduled firing's error is below.
            try:
                now_eager = self.clock.now(self._tzinfo)
                await self._execute_distributed(now_eager, eager=True)
            except Exception as e:
                self._log.error(f"Error in distributed cron loop for {self.method_name}: {e}")
                self.error_count += 1
                await self._back_off()

        while self.is_running:
            try:
                occurrence = await self._wait_for_the_next_occurrence()
                if occurrence is None:
                    break
                target_time, _ = occurrence

                await self._execute_distributed(target_time, eager=False)

            except Exception as e:
                self._log.error(f"Error in distributed cron loop for {self.method_name}: {e}")
                self.error_count += 1
                await self._back_off()

    def get_stats(self) -> dict[str, Any]:
        stats = super().get_stats()
        stats["distributed"] = True
        stats["bucket"] = self.bucket
        stats["lease_ttl"] = self.lease_ttl
        stats["no_overlap"] = self.no_overlap
        return stats

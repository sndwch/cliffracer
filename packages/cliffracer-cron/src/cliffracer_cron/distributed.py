"""Distributed leader-elected cron scheduling using NATS JetStream Key-Value store.

Guarantees that across multiple horizontally scaled service replicas, exactly one
replica executes the scheduled cron job per interval, with active lease locking
to prevent overlapping executions of long-running tasks.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import nats.js.errors
from croniter import croniter
from loguru import logger

from cliffracer.core.exceptions import ConfigurationError

from .cron import CronTimer

# Valid NATS KV key regex: ^[-/_=\.a-zA-Z0-9]+$ (no colons allowed)
_KV_KEY_SAFE_RE = re.compile(r"[^-_=\.a-zA-Z0-9]")


def _sanitize_key_part(part: str) -> str:
    """Sanitize key part to conform to NATS KV valid key regex (no colons)."""
    return _KV_KEY_SAFE_RE.sub("_", part)


class DistributedCronTimer(CronTimer):
    """A CronTimer that coordinates across cluster replicas using cliffracer-kv."""

    def __init__(
        self,
        expression: str,
        tz: str = "UTC",
        eager: bool = False,
        max_drift: float = 1.0,
        error_backoff: float = 5.0,
        headers: dict[str, str] | None = None,
        token_factory: Callable[[], str] | None = None,
        *,
        distributed: bool = True,
        bucket: str = "cron_locks",
        lease_ttl: float = 300.0,
        no_overlap: bool = True,
        kv_extension: Any = None,
    ) -> None:
        super().__init__(
            expression=expression,
            tz=tz,
            eager=eager,
            max_drift=max_drift,
            error_backoff=error_backoff,
            headers=headers,
            token_factory=token_factory,
        )
        self.distributed = distributed
        self.bucket = bucket
        self.lease_ttl = lease_ttl
        self.no_overlap = no_overlap
        self._explicit_kv = kv_extension

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
        )
        c.method_name = self.method_name
        return c

    @property
    def _schedule_description(self) -> str:
        return f"distributed cron '{self.expression}' [{self.tz}] in bucket '{self.bucket}'"

    def _find_kv_extension(self, service: Any) -> Any:
        """Resolve the KvExtension on the service or explicit handle."""
        if self._explicit_kv is not None:
            return self._explicit_kv

        if hasattr(service, "kv") and service.kv is not None:
            return service.kv

        container = getattr(service, "container", None)
        if container is not None and hasattr(container, "extensions"):
            for ext in container.extensions:
                if getattr(ext, "name", None) == "kv":
                    return ext

        return None

    async def _get_raw_bucket(self) -> Any:
        """Retrieve the raw nats.js.kv.KeyValue handle, configuring TTL if possible."""
        kv_ext = self._find_kv_extension(self.service_instance)
        if kv_ext is None:
            raise ConfigurationError(
                f"Handler '{self.method_name}' declares @cron(distributed=True), "
                f"but no KvExtension is registered on service. "
                f"Distributed cron requires cliffracer-kv to be bound to the service."
            )

        # Pre-configure bucket with lease_ttl if _bucket_configs is present
        if hasattr(kv_ext, "_bucket_configs") and kv_ext._bucket_configs is not None:
            if self.bucket not in kv_ext._bucket_configs:
                try:
                    from cliffracer_kv.config import BucketConfig

                    kv_ext._bucket_configs[self.bucket] = BucketConfig(
                        name=self.bucket, ttl=max(self.lease_ttl, 300.0)
                    )
                except ImportError:
                    pass

        return await kv_ext.get_bucket(self.bucket)

    async def start(self, service_instance: Any) -> None:
        """Start the distributed timer, verifying KvExtension presence."""
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

        await super().start(service_instance)

    async def _execute_distributed(self, target_time: datetime, eager: bool = False) -> None:
        """Attempt to atomically acquire interval lock and execute the job."""
        raw_bucket = await self._get_raw_bucket()
        service_name = getattr(getattr(self.service_instance, "config", None), "name", "cliffracer")
        method_name = self.method_name or "unknown"
        safe_service = _sanitize_key_part(service_name)
        safe_method = _sanitize_key_part(method_name)

        epoch_str = "eager" if eager else str(int(target_time.timestamp()))

        active_key = f"cron.{safe_service}.{safe_method}.active"
        interval_key = f"cron.{safe_service}.{safe_method}.{epoch_str}"

        # 1. Overlap check via active lease
        if self.no_overlap:
            try:
                active_entry = await raw_bucket.get(active_key)
                if active_entry and getattr(active_entry, "value", None):
                    active_data = json.loads(active_entry.value.decode("utf-8"))
                    if active_data.get("status") == "running":
                        started_at = float(active_data.get("started_at", 0))
                        if (time.time() - started_at) < self.lease_ttl:
                            logger.warning(
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
                if "not found" not in str(e).lower() and "deleted" not in str(e).lower():
                    logger.warning(f"Error checking active lease {active_key}: {e}")

        # 2. Atomic interval lock acquisition
        instance_id = (
            getattr(self.service_instance, "instance_id", None)
            or getattr(self.service_instance, "_instance_id", None)
            or f"replica-{uuid.uuid4().hex[:8]}"
        )
        payload = {
            "service": service_name,
            "method": method_name,
            "replica": instance_id,
            "status": "running",
            "target_time": target_time.isoformat(),
            "started_at": time.time(),
        }
        payload_bytes = json.dumps(payload).encode("utf-8")

        try:
            rev = await raw_bucket.create(interval_key, payload_bytes)
        except nats.js.errors.KeyWrongLastSequenceError:
            logger.info(
                f"Distributed cron '{method_name}' for epoch {epoch_str} already acquired by peer replica. Skipping."
            )
            return

        # 3. We won the lock! Set active lease if no_overlap
        if self.no_overlap:
            try:
                await raw_bucket.put(active_key, payload_bytes)
            except Exception as e:
                logger.warning(f"Failed to record active lease for {method_name}: {e}")

        # 4. Execute the method
        exec_error: str | None = None
        t0 = time.time()
        try:
            await self._execute_method()
        except Exception as exc:
            exec_error = str(exc)
            raise
        finally:
            # 5. Update interval record with completion status and duration.
            # Do NOT delete the interval record: it must persist so trailing replicas do not re-execute!
            duration_ms = (time.time() - t0) * 1000.0
            payload["status"] = "failed" if exec_error else "completed"
            payload["completed_at"] = time.time()
            payload["duration_ms"] = duration_ms
            if exec_error:
                payload["error"] = exec_error

            try:
                await raw_bucket.update(interval_key, json.dumps(payload).encode("utf-8"), last=rev)
            except Exception as e:
                logger.warning(f"Failed to update cron completion record {interval_key}: {e}")

            # 6. Clear active lease
            if self.no_overlap:
                try:
                    await raw_bucket.delete(active_key)
                except Exception as e:
                    logger.warning(f"Failed to clear active lease {active_key}: {e}")

    async def _timer_loop(self) -> None:
        """Main distributed cron execution loop."""
        if self.eager:
            try:
                now_eager = datetime.now(self._tzinfo)
                await self._execute_distributed(now_eager, eager=True)
            except Exception as e:
                logger.error(f"Error in eager distributed cron for {self.method_name}: {e}")

        while self.is_running:
            try:
                now = datetime.now(self._tzinfo)
                # Compute deterministic target fire time
                target_time = croniter(self.expression, now).get_next(datetime)
                sleep_time = (target_time - now).total_seconds()

                if sleep_time > 0:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
                        # Stop event was set
                        break
                    except TimeoutError:
                        pass

                if self._stop_event.is_set():
                    break

                await self._execute_distributed(target_time, eager=False)

            except Exception as e:
                logger.error(f"Error in distributed cron loop for {self.method_name}: {e}")
                self.error_count += 1
                await asyncio.sleep(self.error_backoff)

    def get_stats(self) -> dict[str, Any]:
        stats = super().get_stats()
        stats["distributed"] = True
        stats["bucket"] = self.bucket
        stats["lease_ttl"] = self.lease_ttl
        stats["no_overlap"] = self.no_overlap
        return stats

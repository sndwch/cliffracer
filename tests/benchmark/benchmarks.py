"""Core benchmarking battery implementation for Cliffracer.

Tracks latency, throughput, serialization, and extension overheads across:
1. Core RPC concurrency (10, 100, 1000)
2. Core JetStream batch pull consumption
3. Serialization (JSON vs MsgPack at 1KB, 100KB, 1MB)
4. cliffracer-auth token validation & password verification
5. cliffracer-kv bulk throughput and stress to failure
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import subprocess
import time
import types
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import nats
from loguru import logger
from nats.errors import MaxPayloadError
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.validation import (
    CONTENT_TYPE_JSON,
    deserialize_payload,
    pack_msgpack,
    serialize_payload,
    unpack_msgpack,
)

DEFAULT_NATS_URL = os.environ.get("CLIFFRACER_TEST_NATS_URL", "nats://localhost:4222")


def get_git_commit() -> str:
    """Return current git commit hash or 'unknown'."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


# ==============================================================================
# 1. Core RPC Benchmarks
# ==============================================================================


class BenchmarkEchoResponse(BaseModel):
    status: str = "ok"
    result: int = 42


class BenchmarkRpcService(CliffracerService):
    @rpc
    async def echo(self, value: int = 42) -> BenchmarkEchoResponse:
        return BenchmarkEchoResponse(status="ok", result=value)


@contextlib.contextmanager
def _cliffracer_logging_silenced() -> Iterator[None]:
    """Silence cliffracer's own logs for one measurement.

    Loguru's enable/disable is process-global and shared with every other test
    in the session, so it has to be paired by a context manager rather than by
    a `finally` further down: anything that raises between the disable and the
    try -- `svc.start()` against an absent broker, say -- would otherwise leave
    cliffracer logging off for the rest of the run, and the damage would land
    in an unrelated test.
    """
    logger.disable("cliffracer")
    try:
        yield
    finally:
        logger.enable("cliffracer")


async def benchmark_rpc(
    nats_url: str = DEFAULT_NATS_URL,
    concurrency_levels: tuple[int, ...] = (10, 100, 1000),
) -> dict[str, dict[str, float]]:
    """Measure Core RPC latency (p50/p95/p99) and throughput at concurrency levels."""
    results: dict[str, dict[str, float]] = {}

    with _cliffracer_logging_silenced():
        svc_name = f"bench_rpc_{uuid.uuid4().hex[:8]}"
        config = ServiceConfig(name=svc_name, nats_url=nats_url)
        svc = BenchmarkRpcService(config)
        await svc.start()
        try:
            # Warmup connection and service dispatch pipeline
            for _ in range(50):
                res = await svc.call_rpc(svc_name, "echo", value=42)
                assert res == {"status": "ok", "result": 42}

            for c in concurrency_levels:
                if c <= 10:
                    total_reqs = max(c * 20, 200)
                elif c <= 100:
                    total_reqs = max(c * 10, 1000)
                else:
                    total_reqs = max(c, 1000)

                async def send_req(s: asyncio.Semaphore) -> float:
                    async with s:
                        t0 = time.perf_counter()
                        resp = await svc.call_rpc(svc_name, "echo", value=42)
                        lat_ms = (time.perf_counter() - t0) * 1000.0
                        assert resp == {"status": "ok", "result": 42}
                        return lat_ms

                # Run 3 trials to eliminate OS scheduling jitter and record the best run
                best_throughput = 0.0
                best_latencies: list[float] = []

                for _ in range(3):
                    sem = asyncio.Semaphore(c)
                    start = time.perf_counter()
                    latencies = list(
                        await asyncio.gather(*[send_req(sem) for _ in range(total_reqs)])
                    )
                    total_time = time.perf_counter() - start
                    throughput = total_reqs / total_time if total_time > 0 else 0.0
                    if throughput > best_throughput:
                        best_throughput = throughput
                        best_latencies = latencies

                best_latencies.sort()
                p50 = best_latencies[int(len(best_latencies) * 0.50)]
                p95 = best_latencies[int(len(best_latencies) * 0.95)]
                p99 = best_latencies[int(len(best_latencies) * 0.99)]

                results[f"concurrency_{c}"] = {
                    "throughput_msgs_sec": round(best_throughput, 1),
                    "p50_latency_ms": round(p50, 3),
                    "p95_latency_ms": round(p95, 3),
                    "p99_latency_ms": round(p99, 3),
                }
        finally:
            try:
                await svc.stop()
            except Exception:
                pass

    return results


# ==============================================================================
# 2. Core JetStream Benchmarks
# ==============================================================================


async def benchmark_jetstream(
    nats_url: str = DEFAULT_NATS_URL,
    total_messages: int = 20000,
    batch_size: int = 250,
) -> dict[str, Any]:
    """Measure JetStream batch pull consumption throughput and latency."""
    nc = await nats.connect(nats_url)
    js = nc.jetstream()

    stream_name = f"BENCH_{uuid.uuid4().hex[:8]}"
    subject = f"{stream_name}.events"

    await js.add_stream(name=stream_name, subjects=[f"{stream_name}.>"])
    payload = b'{"event": "telemetry", "val": 100}'

    best_throughput = 0.0
    best_p50 = 0.0
    best_consumed = 0
    best_acked = 0

    try:
        # Run 3 trials to eliminate OS scheduling jitter and record the best run
        for _ in range(3):
            # Purge stream before each trial to measure clean runs
            await js.purge_stream(stream_name)

            # Pre-publish messages concurrently in chunks of 1000
            batch_pub = 1000
            for i in range(0, total_messages, batch_pub):
                chunk = min(batch_pub, total_messages - i)
                await asyncio.gather(*[js.publish(subject, payload) for _ in range(chunk)])

            consumer_durable = f"pull_{uuid.uuid4().hex[:6]}"
            psub = await js.pull_subscribe(subject, consumer_durable)

            batch_latencies: list[float] = []
            consumed = 0
            t0 = time.perf_counter()

            while consumed < total_messages:
                b_start = time.perf_counter()
                fetch_batch = min(batch_size, total_messages - consumed)
                msgs = await psub.fetch(batch=fetch_batch, timeout=3.0)
                batch_lat = (time.perf_counter() - b_start) * 1000.0
                batch_latencies.append(batch_lat)

                for msg in msgs:
                    await msg.ack()
                    consumed += 1

            total_time = time.perf_counter() - t0
            throughput = consumed / total_time if total_time > 0 else 0.0

            batch_latencies.sort()
            p50 = batch_latencies[len(batch_latencies) // 2] if batch_latencies else 0.0

            await nc.flush()
            acked = 0
            for _ in range(50):
                c_info = await js.consumer_info(stream_name, consumer_durable)
                acked = c_info.ack_floor.consumer_seq if c_info.ack_floor else 0
                if acked >= consumed or c_info.num_ack_pending == 0:
                    break
                await asyncio.sleep(0.02)

            if throughput > best_throughput or best_consumed == 0:
                best_throughput = throughput
                best_p50 = p50
                best_consumed = consumed
                best_acked = acked

        return {
            "throughput_msgs_sec": round(best_throughput, 1),
            "p50_batch_latency_ms": round(best_p50, 3),
            "messages_consumed": best_consumed,
            "messages_acked": best_acked,
            "batch_size": batch_size,
        }
    finally:
        try:
            await js.delete_stream(stream_name)
        except Exception:
            pass
        await nc.close()


# ==============================================================================
# 3. Serialization Benchmarks (JSON vs MessagePack)
# ==============================================================================


def benchmark_serialization(
    payload_specs: tuple[tuple[str, int, int], ...] = (
        ("1KB", 1024, 100),
        ("100KB", 100 * 1024, 30),
        ("1MB", 1024 * 1024, 10),
    ),
) -> dict[str, dict[str, Any]]:
    """Compare JSON vs MessagePack serialization/deserialization times and payload sizes."""
    results: dict[str, dict[str, Any]] = {}

    for label, target_size, iterations in payload_specs:
        data_len = max(10, target_size - 120)
        sample_data = {
            "id": "item-998877",
            "type": "benchmark_payload",
            "active": True,
            "tags": ["core", "benchmark", "serialization", "v5"],
            "metadata": {"version": 1, "owner": "cliffracer"},
            "content": "x" * data_len,
        }

        # Warmup
        warmup_json, _ = serialize_payload(sample_data, format="json")
        _ = deserialize_payload(warmup_json, content_type=CONTENT_TYPE_JSON)
        warmup_msgpack = pack_msgpack(sample_data)
        _ = unpack_msgpack(warmup_msgpack)

        # Run 3 trials to eliminate OS scheduling jitter and record the best run
        best_json_ser_ms = float("inf")
        best_json_deser_ms = float("inf")
        best_msgpack_ser_ms = float("inf")
        best_msgpack_deser_ms = float("inf")
        json_bytes = b""
        msgpack_bytes = b""
        deser_json: Any = None
        deser_msgpack: Any = None

        for _ in range(3):
            # JSON Serialization
            t0 = time.perf_counter()
            for _ in range(iterations):
                json_bytes, _ = serialize_payload(sample_data, format="json")
            best_json_ser_ms = min(
                best_json_ser_ms, ((time.perf_counter() - t0) / iterations) * 1000.0
            )

            # JSON Deserialization
            t0 = time.perf_counter()
            for _ in range(iterations):
                deser_json = deserialize_payload(json_bytes, content_type=CONTENT_TYPE_JSON)
            best_json_deser_ms = min(
                best_json_deser_ms, ((time.perf_counter() - t0) / iterations) * 1000.0
            )

            # MessagePack Serialization
            t0 = time.perf_counter()
            for _ in range(iterations):
                msgpack_bytes = pack_msgpack(sample_data)
            best_msgpack_ser_ms = min(
                best_msgpack_ser_ms, ((time.perf_counter() - t0) / iterations) * 1000.0
            )

            # MessagePack Deserialization
            t0 = time.perf_counter()
            for _ in range(iterations):
                deser_msgpack = unpack_msgpack(msgpack_bytes)
            best_msgpack_deser_ms = min(
                best_msgpack_deser_ms, ((time.perf_counter() - t0) / iterations) * 1000.0
            )

        # Round-trip validation
        assert deser_json == sample_data, f"JSON round-trip validation failed for {label}"
        assert deser_msgpack == sample_data, f"MessagePack round-trip validation failed for {label}"

        speedup_ser = (
            round(best_json_ser_ms / best_msgpack_ser_ms, 2) if best_msgpack_ser_ms > 0 else 1.0
        )
        speedup_deser = (
            round(best_json_deser_ms / best_msgpack_deser_ms, 2)
            if best_msgpack_deser_ms > 0
            else 1.0
        )

        results[label] = {
            "json_ser_ms": round(best_json_ser_ms, 4),
            "json_deser_ms": round(best_json_deser_ms, 4),
            "json_size_bytes": len(json_bytes),
            "msgpack_ser_ms": round(best_msgpack_ser_ms, 4),
            "msgpack_deser_ms": round(best_msgpack_deser_ms, 4),
            "msgpack_size_bytes": len(msgpack_bytes),
            "msgpack_ser_speedup": speedup_ser,
            "msgpack_deser_speedup": speedup_deser,
        }

    return results


# ==============================================================================
# 4. Extension: cliffracer-auth Benchmarks
# ==============================================================================


def benchmark_auth(token_iterations: int = 5000, hash_iterations: int = 5) -> dict[str, Any]:
    """Measure cliffracer-auth token validation and password verification overhead."""
    from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

    auth_svc = SimpleAuthService(
        AuthConfig(
            secret_key="benchmarking-secret-key-at-least-32-chars-long!",
            algorithm="HS256",
        )
    )
    _ = auth_svc.create_user(
        "admin_bench",
        "admin@bench.local",
        "pass12345",
        roles={"admin", "operator"},
        permissions={"read", "write"},
    )
    token = auth_svc.authenticate("admin_bench", "pass12345")
    assert token is not None

    # Warmup
    for _ in range(min(token_iterations // 10, 100)):
        _ = auth_svc.validate_token(token)

    # Token Validation & Role Checking (best of 3 trials)
    val_trials: list[float] = []
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(token_iterations):
            ctx = auth_svc.validate_token(token)
            assert ctx is not None
            assert ctx.user is not None
            assert "admin" in ctx.user.roles
        dur_val = time.perf_counter() - t0
        if dur_val > 0:
            val_trials.append(token_iterations / dur_val)
    token_val_ops = max(val_trials) if val_trials else 0.0

    # Password Hash Verification (Modern per-user salt PBKDF2) (best of 3 trials)
    modern_trials: list[float] = []
    modern_hash = auth_svc.hash_password("pass12345")
    assert not auth_svc.verify_password("wrong-password", modern_hash)
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(hash_iterations):
            assert auth_svc.verify_password("pass12345", modern_hash)
        dur_mod = time.perf_counter() - t0
        if dur_mod > 0:
            modern_trials.append(hash_iterations / dur_mod)
    modern_hash_ops = max(modern_trials) if modern_trials else 0.0

    # Legacy Password Hash Verification (best of 3 trials)
    legacy_trials: list[float] = []
    legacy_hash = auth_svc._legacy_hash("pass12345")
    assert not auth_svc.verify_password("wrong-password", legacy_hash)
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(hash_iterations):
            assert auth_svc.verify_password("pass12345", legacy_hash)
        dur_leg = time.perf_counter() - t0
        if dur_leg > 0:
            legacy_trials.append(hash_iterations / dur_leg)
    legacy_hash_ops = max(legacy_trials) if legacy_trials else 0.0

    return {
        "token_validation_ops_sec": round(token_val_ops, 1),
        "modern_hash_ops_sec": round(modern_hash_ops, 2),
        "legacy_hash_ops_sec": round(legacy_hash_ops, 2),
    }


# ==============================================================================
# 5. Extension: cliffracer-kv Bulk Throughput & Stress to Failure
# ==============================================================================


async def benchmark_kv(nats_url: str = DEFAULT_NATS_URL, num_items: int = 5000) -> dict[str, Any]:
    """Measure cliffracer-kv bulk throughput and stress to failure behavior."""
    from cliffracer_kv import KvExtension

    nc = await nats.connect(nats_url)
    js = nc.jetstream()

    bucket_name = f"b_{uuid.uuid4().hex[:8]}"
    kv = KvExtension(buckets=[bucket_name], create_if_missing=True, nc=nc, js=js)
    ctx = cast(Any, types.SimpleNamespace(nc=nc, js=js))
    await kv.setup(ctx)

    latencies: list[float] = []

    try:
        # 1. Bulk Put (concurrent / pipelined across trials)
        put_trials: list[float] = []
        last_put_results: list[int] = []
        for _ in range(3):
            t0 = time.perf_counter()
            put_results = await asyncio.gather(
                *[
                    kv.put(
                        bucket_name,
                        f"bench_key_{i}",
                        {"index": i, "data": "val" * 50, "valid": True},
                    )
                    for i in range(num_items)
                ]
            )
            elapsed = time.perf_counter() - t0
            committed_count = len([r for r in put_results if isinstance(r, int) and r > 0])
            put_trials.append(committed_count / elapsed if elapsed > 0 else 0.0)
            last_put_results = put_results
        put_ops = max(put_trials) if put_trials else 0.0
        committed_puts = len([r for r in last_put_results if isinstance(r, int) and r > 0])

        # Assertions on put revisions
        assert committed_puts == num_items, (
            f"Expected {num_items} committed puts, got {committed_puts}"
        )
        assert all(isinstance(r, int) and r > 0 for r in last_put_results), (
            "All revisions must be positive integers"
        )

        # 2. Bulk Get (concurrent / pipelined across trials)
        get_trials: list[float] = []
        last_vals: list[Any] = []
        for _ in range(3):
            t0 = time.perf_counter()
            vals = await asyncio.gather(
                *[kv.get(bucket_name, f"bench_key_{i}") for i in range(num_items)]
            )
            elapsed = time.perf_counter() - t0
            valid_reads = [v for v in vals if v is not None]
            get_trials.append(len(valid_reads) / elapsed if elapsed > 0 else 0.0)
            last_vals = vals
        get_ops = max(get_trials) if get_trials else 0.0

        # Assert non-None gets, exact length, and round-trip data consistency
        assert all(v is not None for v in last_vals), "Cache misses detected in bulk get"
        assert len(last_vals) == num_items, f"Expected {num_items} items, got {len(last_vals)}"
        for i, val in enumerate(last_vals):
            assert isinstance(val, dict), f"Item {i} is not a dict"
            assert val.get("index") == i, f"Item {i} index mismatch: {val.get('index')}"
            assert val.get("valid") is True, f"Item {i} valid flag mismatch"

        # Sample individual round-trip latencies
        for i in range(min(num_items, 250)):
            p0 = time.perf_counter()
            _ = await kv.get(bucket_name, f"bench_key_{i}")
            latencies.append((time.perf_counter() - p0) * 1000.0)
        latencies.sort()
        p50 = latencies[len(latencies) // 2] if latencies else 0.0

        # 3. Stress to Failure & Graceful Degradation Check
        # Attempt oversized payload (> 2MB) intentionally to trigger NATS size failure
        stress_failure_handled = False
        oversized_payload = {"huge": "M" * (2 * 1024 * 1024)}
        try:
            await kv.put(bucket_name, "huge_key", oversized_payload)
        except MaxPayloadError:
            # The failure this check is for: the server's payload limit, refused by name. Any
            # other exception (a serialization error, a closed connection, a missing bucket) is
            # not graceful degradation under stress, so it propagates and fails the benchmark.
            stress_failure_handled = True

        # 4. Verify Recovery (No Corrupted Zombie State)
        recovery_verified = False
        await kv.put(bucket_name, "recovery_test", {"recovered": True})
        rec_val = await kv.get(bucket_name, "recovery_test")
        if rec_val and rec_val.get("recovered") is True:
            recovery_verified = True

        return {
            "bulk_put_ops_sec": round(put_ops, 1),
            "bulk_get_ops_sec": round(get_ops, 1),
            "p50_latency_ms": round(p50, 3),
            "items_processed": committed_puts,
            "stress_failure_handled": stress_failure_handled,
            "recovery_verified": recovery_verified,
        }
    finally:
        try:
            await js.delete_key_value(bucket_name)
        except Exception:
            pass
        await nc.close()


# ==============================================================================
# Environment & Node Introspection Context
# ==============================================================================


#: The memory cap of the control group this process runs in, which on a runner's job container is
#: the cap the runner set. `psutil` reads the host's total instead, so a change to the cap does not
#: show in `total_ram_gb`.
CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")


def cgroup_memory_limit_gb(path: Path | None = None) -> float | None:
    """The control group's memory limit in GiB, or None when it has none or cannot be read.

    The file holds a byte count, or `max` for no limit. A missing file (no cgroup v2 here, or not
    on Linux) and a value that is not a number are both "no figure", and the block records null for
    them, as it does for a host that cannot report its load.
    """
    try:
        text = (CGROUP_MEMORY_MAX if path is None else path).read_text().strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return round(int(text) / (1024**3), 2)


def get_environment_context(nats_url: str = DEFAULT_NATS_URL) -> dict[str, Any]:
    """Capture node specifications, runner hardware, NATS allocations, and network topology."""
    import urllib.parse
    import urllib.request

    import psutil

    # 1. Runner Node Specifications
    vm = psutil.virtual_memory()
    # The first five describe the hardware class and are what the regression checker compares. The
    # rest (the memory limit, the load, the runner name) describe the conditions this particular run
    # was taken under and differ between runs, so RUNNER_SPEC_FIELDS in
    # scripts/check_benchmark_regression.py names which are which: a number here that varies per run
    # would otherwise read as a different machine and the checker would refuse to score every run.
    runner_info: dict[str, Any] = {
        "cpu_count": os.cpu_count() or 1,
        "cpu_arch": platform.machine(),
        "total_ram_gb": round(vm.total / (1024**3), 2),
        "os": f"{platform.system()} {platform.release()}",
        "python_version": platform.python_version(),
    }
    # A condition of this run, not a hardware class: kept out of RUNNER_SPEC_FIELDS, so a cap that
    # differs from the baseline's is on record without refusing to score the run.
    runner_info["memory_limit_gb"] = cgroup_memory_limit_gb()

    # What else the host was doing. A benchmark taken on a busy host reads as a
    # regression, so the number travels with the load it was measured under.
    try:
        one, five, fifteen = os.getloadavg()
        runner_info["load_average"] = {
            "1min": round(one, 2),
            "5min": round(five, 2),
            "15min": round(fifteen, 2),
        }
    except OSError:
        runner_info["load_average"] = None

    runner_name = os.environ.get("RUNNER_NAME") or os.environ.get("GITEA_RUNNER_NAME")
    if runner_name:
        runner_info["runner_name"] = runner_name

    # 2. Network Topology Context
    parsed = urllib.parse.urlparse(nats_url)
    host = parsed.hostname or "localhost"
    is_local = host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
    topology = (
        "Localhost / Loopback (collocated zero-hop IPC/TCP)"
        if is_local
        else f"Remote / Bridged network ({host})"
    )
    network_context = {
        "nats_url": nats_url,
        "target_host": host,
        "port": parsed.port or 4222,
        "is_collocated": is_local,
        "topology": topology,
    }

    # 3. NATS Server Specifications & Resource Allocations
    nats_info: dict[str, Any] = {}

    # Query monitoring port if available (default 8222)
    try:
        req = urllib.request.Request(
            f"http://{host}:8222/varz",
            headers={"User-Agent": "cliffracer-benchmark"},
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            varz = json.loads(resp.read().decode("utf-8"))
            nats_info["version"] = varz.get("version", "unknown")
            nats_info["server_id"] = varz.get("server_id", "unknown")
            nats_info["jetstream_enabled"] = varz.get("jetstream") is not None
            nats_info["max_payload_bytes"] = varz.get("max_payload", 1048576)
            nats_info["memory_usage_bytes"] = varz.get("mem", 0)
            nats_info["cpu_pct"] = varz.get("cpu", 0.0)
            nats_info["max_connections"] = varz.get("max_connections", 65536)
            nats_info["active_connections"] = varz.get("connections", 0)
    except Exception as exc:
        nats_info = {
            "probe_failed": f"{type(exc).__name__}: {exc}",
        }

    return {
        "runner": runner_info,
        "network": network_context,
        "nats": nats_info,
    }


# ==============================================================================
# Master Benchmark Aggregator
# ==============================================================================


async def run_all_benchmarks(
    nats_url: str = DEFAULT_NATS_URL,
    include_extensions: bool = True,
) -> dict[str, Any]:
    """Execute complete benchmarking battery and return structured dictionary."""
    metrics: dict[str, Any] = {}
    failures: dict[str, str] = {}

    # 1. Core RPC
    try:
        metrics["rpc"] = await benchmark_rpc(nats_url)
    except Exception as exc:
        failures["rpc"] = f"{type(exc).__name__}: {exc}"
        metrics["rpc"] = {"failed": True, "error": str(exc)}

    # 2. JetStream
    try:
        metrics["jetstream"] = await benchmark_jetstream(nats_url)
    except Exception as exc:
        failures["jetstream"] = f"{type(exc).__name__}: {exc}"
        metrics["jetstream"] = {"failed": True, "error": str(exc)}

    # 3. Serialization
    try:
        metrics["serialization"] = benchmark_serialization()
    except Exception as exc:
        failures["serialization"] = f"{type(exc).__name__}: {exc}"
        metrics["serialization"] = {"failed": True, "error": str(exc)}

    if include_extensions:
        # 4. Auth
        try:
            metrics["auth"] = benchmark_auth()
        except Exception as exc:
            failures["auth"] = f"{type(exc).__name__}: {exc}"
            metrics["auth"] = {"failed": True, "error": str(exc)}

        # 5. KV Throughput & Stress
        try:
            metrics["kv"] = await benchmark_kv(nats_url)
        except Exception as exc:
            failures["kv"] = f"{type(exc).__name__}: {exc}"
            metrics["kv"] = {"failed": True, "error": str(exc)}

    result: dict[str, Any] = {
        "version": "1.0.0",
        "timestamp": datetime.now(UTC).isoformat(),
        "git_commit": get_git_commit(),
        "platform": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "environment": get_environment_context(nats_url),
        "metrics": metrics,
    }
    if failures:
        result["failures"] = failures

    return result

"""In-memory dispatch timing and throughput tracking."""

import math
import time
from collections import deque
from typing import Any

#: Connection events that add one to the count of the same name.
CONNECTION_COUNTERS = ("total_connections", "failed_connections", "reconnections")


class PerformanceMetrics:
    """Rolling window metrics collector tracking latency and request outcomes.

    `targets` holds the four thresholds `check_performance_targets()` judges against, and it is
    a plain dict you may replace entries of: `max_latency_ms` (10.0, against p95),
    `min_success_rate` (99.0, a percentage), `max_memory_mb` (500.0, against the current sample)
    and `min_throughput_rps` (100.0, against the average over the last minute's completed seconds).
    They are defaults, not recommendations: pick the ones that mean something for your service.

    The latency, success-rate and memory checks are only reported once there is data for them;
    the throughput check is always reported. So an instance that has recorded nothing, or a
    service that serves fewer requests a second than `min_throughput_rps`, reports
    `overall.passing` as false until that target is set to its own rate.
    """

    def __init__(self, history_size: int = 1000):
        """
        Initialize performance metrics collector.

        Args:
            history_size: Number of recent metrics to keep in memory
        """
        self.history_size = history_size

        # Latency tracking
        self._latencies: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._request_counts = {"success": 0, "error": 0, "timeout": 0}

        # Throughput tracking
        self._throughput_window: deque[int] = deque(maxlen=60)  # 60 seconds of data
        self._current_second_count = 0
        # The second `_current_second_count` is for; None until the first request, so the idle
        # time before traffic begins is not a zero in the average.
        self._last_second: int | None = None

        # Memory and resource tracking
        self._memory_samples: deque[dict[str, float]] = deque(maxlen=history_size)
        self._cpu_samples: deque[dict[str, float]] = deque(maxlen=history_size)

        # Connection tracking
        self._connection_stats = {
            "total_connections": 0,
            "active_connections": 0,
            "failed_connections": 0,
            "reconnections": 0,
        }

        # Custom metrics
        self._custom_metrics: dict[str, dict[str, Any]] = {}
        self._counters: dict[str, int] = {}

        # Performance targets
        self.targets = {
            "max_latency_ms": 10.0,
            "min_success_rate": 99.0,
            "max_memory_mb": 500.0,
            "min_throughput_rps": 100.0,
        }

    def record_latency(
        self, latency_ms: float, success: bool = True, timeout: bool = False
    ) -> None:
        """Record request latency and outcome.

        The outcome is decided once: a timeout is a timeout whatever `success` says, otherwise
        it is a success or an error. Every success rate this reports counts the same way.
        """
        current_time = time.time()
        outcome = "timeout" if timeout else "success" if success else "error"

        self._latencies.append(
            {
                "latency_ms": latency_ms,
                "timestamp": current_time,
                "outcome": outcome,
                "success": outcome == "success",
                "timeout": outcome == "timeout",
            }
        )
        self._request_counts[outcome] += 1

        self._advance_throughput_window(current_time)
        self._current_second_count += 1

    def _advance_throughput_window(self, now: float) -> None:
        """Close every second that has passed since the last one seen, idle ones as zeros.

        Called by a write and by a read, so the figures follow the clock and not only the
        traffic: a window read an hour after the last request is an hour of zeros, not the
        last busy second.
        """
        second = int(now)
        if self._last_second is None:
            self._last_second = second
            return
        gap = second - self._last_second
        if gap <= 0:
            return
        self._throughput_window.append(self._current_second_count)
        # The window holds 60 seconds, so no more than that many idle ones can matter.
        for _ in range(min(gap - 1, self._throughput_window.maxlen or 60)):
            self._throughput_window.append(0)
        self._current_second_count = 0
        self._last_second = second

    def record_memory_usage(self, memory_mb: float) -> None:
        """Record memory usage sample"""
        self._memory_samples.append({"memory_mb": memory_mb, "timestamp": time.time()})

    def record_cpu_usage(self, cpu_percent: float) -> None:
        """Record CPU usage sample"""
        self._cpu_samples.append({"cpu_percent": cpu_percent, "timestamp": time.time()})

    def record_connection_event(self, event_type: str) -> None:
        """Record a connection event.

        - `total_connections`, `failed_connections`, `reconnections`: add one to that count.
        - `connection_opened`: add one to `total_connections` and to `active_connections`.
        - `connection_closed`: take one from `active_connections`, never below zero.

        `active_connections` is how many are open now, so it is a level and not an event:
        it moves with the two above, or is set outright by `set_active_connections`. Any
        other name raises `ValueError`, because an event that is recorded nowhere reads as
        one that never happened.
        """
        if event_type in CONNECTION_COUNTERS:
            self._connection_stats[event_type] += 1
        elif event_type == "connection_opened":
            self._connection_stats["total_connections"] += 1
            self._connection_stats["active_connections"] += 1
        elif event_type == "connection_closed":
            self._connection_stats["active_connections"] = max(
                0, self._connection_stats["active_connections"] - 1
            )
        else:
            raise ValueError(
                f"unknown connection event {event_type!r}: record one of "
                f"{', '.join(sorted((*CONNECTION_COUNTERS, 'connection_opened', 'connection_closed')))}"
                + (
                    "; active_connections is a level, so use connection_opened, "
                    "connection_closed or set_active_connections"
                    if event_type == "active_connections"
                    else ""
                )
            )

    def set_active_connections(self, count: int) -> None:
        """Set how many connections are open now, for a caller that counts them itself."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"active connections must be an integer of 0 or more, got {count!r}")
        self._connection_stats["active_connections"] = count

    def increment_counter(self, name: str, value: int = 1) -> None:
        """Increment a custom counter"""
        self._counters[name] = self._counters.get(name, 0) + value

    def set_gauge(self, name: str, value: float) -> None:
        """Set a custom gauge metric"""
        self._custom_metrics[name] = {"value": value, "timestamp": time.time()}

    def record_custom_metric(self, name: str, value: float) -> None:
        """Record a custom value, which is read back as a gauge: the last one recorded."""
        self.set_gauge(name, value)

    def get_latency_stats(self) -> dict[str, Any]:
        """Get latency statistics over the last `history_size` requests.

        `success_rate_percent` here is the share of those requests that succeeded, a timeout
        not being one; `get_throughput_stats` reports the same share over every request since
        the last reset.
        """
        if not self._latencies:
            return {"error": "No latency data available"}

        latencies = [metric["latency_ms"] for metric in self._latencies]
        successful_latencies = [
            metric["latency_ms"] for metric in self._latencies if metric["outcome"] == "success"
        ]

        return {
            "count": len(latencies),
            "mean_ms": sum(latencies) / len(latencies),
            "min_ms": min(latencies),
            "max_ms": max(latencies),
            "median_ms": self._median(latencies),
            "p95_ms": self._percentile(latencies, 0.95),
            "p99_ms": self._percentile(latencies, 0.99),
            "success_count": len(successful_latencies),
            "success_rate_percent": (len(successful_latencies) / len(latencies)) * 100,
            "sub_millisecond_count": len([latency for latency in latencies if latency < 1.0]),
            "sub_millisecond_percent": (
                len([latency for latency in latencies if latency < 1.0]) / len(latencies)
            )
            * 100,
        }

    def get_throughput_stats(self) -> dict[str, Any]:
        """Get throughput statistics.

        `average_rps` and `max_rps` are over the completed seconds of the last minute, idle
        seconds after the first request counted as zero. `current_rps` is the last completed
        second, or the count so far when none has completed; it is zero once traffic stops.
        """
        self._advance_throughput_window(time.time())
        if not self._throughput_window:
            current_rps = self._current_second_count
            avg_rps: float = float(current_rps)
            max_rps = current_rps
        else:
            current_rps = self._throughput_window[-1]
            avg_rps = sum(self._throughput_window) / len(self._throughput_window)
            max_rps = max(self._throughput_window)

        total_requests = sum(self._request_counts.values())

        return {
            "current_rps": current_rps,
            "average_rps": avg_rps,
            "max_rps": max_rps,
            "total_requests": total_requests,
            "success_requests": self._request_counts["success"],
            "error_requests": self._request_counts["error"],
            "timeout_requests": self._request_counts["timeout"],
            "success_rate_percent": (self._request_counts["success"] / total_requests * 100)
            if total_requests > 0
            else 0,
        }

    def get_resource_stats(self) -> dict[str, Any]:
        """Get resource usage statistics"""
        stats: dict[str, dict[str, Any]] = {
            "memory": {"error": "No memory data"},
            "cpu": {"error": "No CPU data"},
        }

        if self._memory_samples:
            memory_values = [s["memory_mb"] for s in self._memory_samples]
            stats["memory"] = {
                "current_mb": memory_values[-1],
                "average_mb": sum(memory_values) / len(memory_values),
                "max_mb": max(memory_values),
                "min_mb": min(memory_values),
            }

        if self._cpu_samples:
            cpu_values = [s["cpu_percent"] for s in self._cpu_samples]
            stats["cpu"] = {
                "current_percent": cpu_values[-1],
                "average_percent": sum(cpu_values) / len(cpu_values),
                "max_percent": max(cpu_values),
                "min_percent": min(cpu_values),
            }

        return stats

    def get_connection_stats(self) -> dict[str, Any]:
        """Get connection statistics"""
        return self._connection_stats.copy()

    def get_custom_metrics(self) -> dict[str, Any]:
        """Get custom metrics and counters"""
        return {"gauges": self._custom_metrics.copy(), "counters": self._counters.copy()}

    def check_performance_targets(self) -> dict[str, Any]:
        """Check if performance targets are being met"""
        latency_stats = self.get_latency_stats()
        throughput_stats = self.get_throughput_stats()
        resource_stats = self.get_resource_stats()

        checks = {}

        # Latency check
        if "p95_ms" in latency_stats:
            checks["latency"] = {
                "target": self.targets["max_latency_ms"],
                "actual": latency_stats["p95_ms"],
                "passing": latency_stats["p95_ms"] <= self.targets["max_latency_ms"],
            }

        # Success rate check
        if "success_rate_percent" in latency_stats:
            checks["success_rate"] = {
                "target": self.targets["min_success_rate"],
                "actual": latency_stats["success_rate_percent"],
                "passing": latency_stats["success_rate_percent"]
                >= self.targets["min_success_rate"],
            }

        # Throughput check
        checks["throughput"] = {
            "target": self.targets["min_throughput_rps"],
            "actual": throughput_stats["average_rps"],
            "passing": throughput_stats["average_rps"] >= self.targets["min_throughput_rps"],
        }

        # Memory check
        if "memory" in resource_stats and "current_mb" in resource_stats["memory"]:
            checks["memory"] = {
                "target": self.targets["max_memory_mb"],
                "actual": resource_stats["memory"]["current_mb"],
                "passing": resource_stats["memory"]["current_mb"] <= self.targets["max_memory_mb"],
            }

        # Overall status
        passing_checks = [check["passing"] for check in checks.values()]
        checks["overall"] = {
            "passing": all(passing_checks),
            "checks_passed": sum(passing_checks),
            "total_checks": len(passing_checks),
        }

        return checks

    def get_performance_summary(self) -> dict[str, Any]:
        """Get comprehensive performance summary"""
        return {
            "timestamp": time.time(),
            "latency": self.get_latency_stats(),
            "throughput": self.get_throughput_stats(),
            "resources": self.get_resource_stats(),
            "connections": self.get_connection_stats(),
            "custom": self.get_custom_metrics(),
            "targets": self.check_performance_targets(),
            "metrics_history_size": len(self._latencies),
        }

    def reset_metrics(self) -> None:
        """Reset all metrics"""
        self._latencies.clear()
        self._request_counts = {"success": 0, "error": 0, "timeout": 0}
        self._throughput_window.clear()
        self._current_second_count = 0
        self._last_second = None
        self._memory_samples.clear()
        self._cpu_samples.clear()
        self._connection_stats = {
            "total_connections": 0,
            "active_connections": 0,
            "failed_connections": 0,
            "reconnections": 0,
        }
        self._custom_metrics.clear()
        self._counters.clear()

    def _median(self, values: list[float]) -> float:
        """Calculate median of values"""
        if not values:
            return 0.0
        sorted_values = sorted(values)
        n = len(sorted_values)
        if n % 2 == 0:
            return (sorted_values[n // 2 - 1] + sorted_values[n // 2]) / 2
        return sorted_values[n // 2]

    def _percentile(self, values: list[float], percentile: float) -> float:
        """Nearest-rank percentile: the smallest value that `percentile` of the values do not exceed.

        `percentile` is a fraction. With 100 values, 0.95 is the 95th smallest; with 20, 0.95
        is the 19th, so a single outlier is the maximum and does not set p95. An empty list is
        0.0.
        """
        if not values:
            return 0.0
        sorted_values = sorted(values)
        rank = math.ceil(percentile * len(sorted_values))
        return sorted_values[min(max(rank, 1), len(sorted_values)) - 1]

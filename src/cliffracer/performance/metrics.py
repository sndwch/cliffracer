"""
Performance metrics collection and monitoring for Cliffracer services
"""

import time
from collections import deque
from typing import Any


class PerformanceMetrics:
    """
    High-performance metrics collector with minimal overhead.

    Collects and aggregates performance metrics for monitoring
    and optimization purposes.
    """

    def __init__(self, history_size: int = 1000):
        """
        Initialize performance metrics collector.

        Args:
            history_size: Number of recent metrics to keep in memory
        """
        self.history_size = history_size

        # Latency tracking
        self._latencies = deque(maxlen=history_size)
        self._request_counts = {"success": 0, "error": 0, "timeout": 0}

        # Throughput tracking
        self._throughput_window = deque(maxlen=60)  # 60 seconds of data
        self._current_second_count = 0
        self._last_second = int(time.time())

        # Memory and resource tracking
        self._memory_samples = deque(maxlen=history_size)
        self._cpu_samples = deque(maxlen=history_size)

        # Connection tracking
        self._connection_stats = {
            "total_connections": 0,
            "active_connections": 0,
            "failed_connections": 0,
            "reconnections": 0
        }

        # Custom metrics
        self._custom_metrics = {}
        self._counters = {}

        # Performance targets
        self.targets = {
            "max_latency_ms": 10.0,
            "min_success_rate": 99.0,
            "max_memory_mb": 500.0,
            "min_throughput_rps": 100.0
        }

    def record_latency(self, latency_ms: float, success: bool = True, timeout: bool = False):
        """Record request latency and outcome"""
        current_time = time.time()

        # Record latency
        self._latencies.append({
            "latency_ms": latency_ms,
            "timestamp": current_time,
            "success": success,
            "timeout": timeout
        })

        # Update request counts
        if timeout:
            self._request_counts["timeout"] += 1
        elif success:
            self._request_counts["success"] += 1
        else:
            self._request_counts["error"] += 1

        # Update throughput tracking
        current_second = int(current_time)
        if current_second != self._last_second:
            self._throughput_window.append(self._current_second_count)
            self._current_second_count = 1
            self._last_second = current_second
        else:
            self._current_second_count += 1

    def record_memory_usage(self, memory_mb: float):
        """Record memory usage sample"""
        self._memory_samples.append({
            "memory_mb": memory_mb,
            "timestamp": time.time()
        })

    def record_cpu_usage(self, cpu_percent: float):
        """Record CPU usage sample"""
        self._cpu_samples.append({
            "cpu_percent": cpu_percent,
            "timestamp": time.time()
        })

    def record_connection_event(self, event_type: str):
        """Record connection-related events"""
        if event_type in self._connection_stats:
            self._connection_stats[event_type] += 1

    def increment_counter(self, name: str, value: int = 1):
        """Increment a custom counter"""
        self._counters[name] = self._counters.get(name, 0) + value

    def set_gauge(self, name: str, value: float):
        """Set a custom gauge metric"""
        self._custom_metrics[name] = {
            "value": value,
            "timestamp": time.time()
        }

    def get_latency_stats(self) -> dict[str, Any]:
        """Get latency statistics"""
        if not self._latencies:
            return {"error": "No latency data available"}

        latencies = [metric["latency_ms"] for metric in self._latencies]
        successful_latencies = [metric["latency_ms"] for metric in self._latencies if metric["success"]]

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
            "sub_millisecond_percent": (len([latency for latency in latencies if latency < 1.0]) / len(latencies)) * 100
        }

    def get_throughput_stats(self) -> dict[str, Any]:
        """Get throughput statistics"""
        if not self._throughput_window:
            current_rps = self._current_second_count
            avg_rps = current_rps
            max_rps = current_rps
        else:
            current_rps = self._current_second_count
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
            "success_rate_percent": (self._request_counts["success"] / total_requests * 100) if total_requests > 0 else 0
        }

    def get_resource_stats(self) -> dict[str, Any]:
        """Get resource usage statistics"""
        stats = {
            "memory": {"error": "No memory data"},
            "cpu": {"error": "No CPU data"}
        }

        if self._memory_samples:
            memory_values = [s["memory_mb"] for s in self._memory_samples]
            stats["memory"] = {
                "current_mb": memory_values[-1],
                "average_mb": sum(memory_values) / len(memory_values),
                "max_mb": max(memory_values),
                "min_mb": min(memory_values)
            }

        if self._cpu_samples:
            cpu_values = [s["cpu_percent"] for s in self._cpu_samples]
            stats["cpu"] = {
                "current_percent": cpu_values[-1],
                "average_percent": sum(cpu_values) / len(cpu_values),
                "max_percent": max(cpu_values),
                "min_percent": min(cpu_values)
            }

        return stats

    def get_connection_stats(self) -> dict[str, Any]:
        """Get connection statistics"""
        return self._connection_stats.copy()

    def get_custom_metrics(self) -> dict[str, Any]:
        """Get custom metrics and counters"""
        return {
            "gauges": self._custom_metrics.copy(),
            "counters": self._counters.copy()
        }

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
                "passing": latency_stats["p95_ms"] <= self.targets["max_latency_ms"]
            }

        # Success rate check
        if "success_rate_percent" in latency_stats:
            checks["success_rate"] = {
                "target": self.targets["min_success_rate"],
                "actual": latency_stats["success_rate_percent"],
                "passing": latency_stats["success_rate_percent"] >= self.targets["min_success_rate"]
            }

        # Throughput check
        checks["throughput"] = {
            "target": self.targets["min_throughput_rps"],
            "actual": throughput_stats["average_rps"],
            "passing": throughput_stats["average_rps"] >= self.targets["min_throughput_rps"]
        }

        # Memory check
        if "memory" in resource_stats and "current_mb" in resource_stats["memory"]:
            checks["memory"] = {
                "target": self.targets["max_memory_mb"],
                "actual": resource_stats["memory"]["current_mb"],
                "passing": resource_stats["memory"]["current_mb"] <= self.targets["max_memory_mb"]
            }

        # Overall status
        passing_checks = [check["passing"] for check in checks.values()]
        checks["overall"] = {
            "passing": all(passing_checks),
            "checks_passed": sum(passing_checks),
            "total_checks": len(passing_checks)
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
            "metrics_history_size": len(self._latencies)
        }

    def reset_metrics(self):
        """Reset all metrics"""
        self._latencies.clear()
        self._request_counts = {"success": 0, "error": 0, "timeout": 0}
        self._throughput_window.clear()
        self._current_second_count = 0
        self._memory_samples.clear()
        self._cpu_samples.clear()
        self._connection_stats = {
            "total_connections": 0,
            "active_connections": 0,
            "failed_connections": 0,
            "reconnections": 0
        }
        self._custom_metrics.clear()
        self._counters.clear()

    def _median(self, values: list) -> float:
        """Calculate median of values"""
        if not values:
            return 0.0
        sorted_values = sorted(values)
        n = len(sorted_values)
        if n % 2 == 0:
            return (sorted_values[n//2 - 1] + sorted_values[n//2]) / 2
        return sorted_values[n//2]

    def _percentile(self, values: list, percentile: float) -> float:
        """Calculate percentile of values"""
        if not values:
            return 0.0
        sorted_values = sorted(values)
        index = int(percentile * len(sorted_values))
        if index >= len(sorted_values):
            index = len(sorted_values) - 1
        return sorted_values[index]

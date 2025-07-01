"""
High-performance service with built-in optimizations
"""

import time
from typing import Any

from loguru import logger

from ..performance.batch_processor import BatchProcessor
from ..performance.connection_pool import OptimizedNATSConnection
from ..performance.metrics import PerformanceMetrics
from .base_service import BaseNATSService
from .service_config import ServiceConfig


class HighPerformanceService(BaseNATSService):
    """
    High-performance service with built-in optimizations.

    This service includes:
    - Optimized NATS connection pooling
    - Automatic batch processing
    - Performance metrics collection
    - Memory and latency optimizations
    """

    def __init__(
        self,
        config: ServiceConfig,
        enable_connection_pooling: bool = True,
        enable_batch_processing: bool = True,
        enable_metrics: bool = True,
        connection_pool_size: int = 5,
        batch_size: int = 50,
        batch_timeout_ms: int = 25
    ):
        """
        Initialize high-performance service.

        Args:
            config: Service configuration
            enable_connection_pooling: Enable optimized connection pooling
            enable_batch_processing: Enable automatic batch processing
            enable_metrics: Enable performance metrics collection
            connection_pool_size: Number of connections in pool
            batch_size: Maximum batch size for processing
            batch_timeout_ms: Batch timeout in milliseconds
        """
        super().__init__(config)

        self.enable_connection_pooling = enable_connection_pooling
        self.enable_batch_processing = enable_batch_processing
        self.enable_metrics = enable_metrics

        # Initialize optimized components
        if enable_connection_pooling:
            self._connection_pool = OptimizedNATSConnection(
                nats_url=config.nats_url,
                max_connections=connection_pool_size
            )
        else:
            self._connection_pool = None

        if enable_batch_processing:
            self._batch_processor = BatchProcessor(
                batch_size=batch_size,
                batch_timeout_ms=batch_timeout_ms
            )
        else:
            self._batch_processor = None

        if enable_metrics:
            self._metrics = PerformanceMetrics()
        else:
            self._metrics = None

        # Response cache for frequently requested data
        self._response_cache = {}
        self._cache_timeout = 1.0  # 1 second cache timeout

    async def start(self):
        """Start service with optimizations"""
        logger.info(f"Starting high-performance service: {self.config.name}")

        # Start base service
        await super().start()

        # Initialize connection pool
        if self._connection_pool:
            await self._connection_pool.connect()
            logger.info("Optimized connection pool initialized")

        # Record startup in metrics
        if self._metrics:
            self._metrics.record_connection_event("total_connections")
            self._metrics.set_gauge("service_uptime", 0)

        logger.info(f"High-performance service {self.config.name} started successfully")

    async def stop(self):
        """Stop service and cleanup optimizations"""
        logger.info(f"Stopping high-performance service: {self.config.name}")

        # Flush any pending batches
        if self._batch_processor:
            await self._batch_processor.flush_all()
            logger.debug("Flushed pending batch operations")

        # Close connection pool
        if self._connection_pool:
            await self._connection_pool.close()
            logger.debug("Closed optimized connection pool")

        # Stop base service
        await super().stop()

        logger.info(f"High-performance service {self.config.name} stopped")

    async def call_rpc_optimized(
        self,
        service: str,
        method: str,
        timeout: float = 5.0,
        use_cache: bool = False,
        **kwargs
    ) -> Any:
        """
        Optimized RPC call with caching and metrics.

        Args:
            service: Target service name
            method: RPC method name
            timeout: Request timeout
            use_cache: Enable response caching
            **kwargs: Method arguments

        Returns:
            RPC response
        """
        start_time = time.perf_counter()
        success = False
        timeout_occurred = False

        try:
            # Check cache first
            if use_cache:
                cache_key = f"{service}.{method}.{hash(str(kwargs))}"
                if cache_key in self._response_cache:
                    cache_entry = self._response_cache[cache_key]
                    if time.time() - cache_entry["timestamp"] < self._cache_timeout:
                        if self._metrics:
                            self._metrics.increment_counter("cache_hits")
                        return cache_entry["response"]
                    else:
                        # Cache expired
                        del self._response_cache[cache_key]

            # Use connection pool if available
            if self._connection_pool:
                subject = f"{service}.rpc.{method}"
                payload = self._serialize_payload(kwargs)
                response_data = await self._connection_pool.request(subject, payload, timeout=timeout)
                response = self._deserialize_response(response_data)
            else:
                # Fall back to standard RPC
                response = await super().call_rpc(service, method, **kwargs)

            # Cache successful response
            if use_cache:
                self._response_cache[cache_key] = {
                    "response": response,
                    "timestamp": time.time()
                }
                if self._metrics:
                    self._metrics.increment_counter("cache_sets")

            success = True
            return response

        except TimeoutError:
            timeout_occurred = True
            raise
        except Exception:
            raise
        finally:
            # Record metrics
            if self._metrics:
                end_time = time.perf_counter()
                latency_ms = (end_time - start_time) * 1000
                self._metrics.record_latency(latency_ms, success, timeout_occurred)

    async def batch_process(
        self,
        batch_key: str,
        item: Any,
        processor_func: callable
    ) -> Any:
        """
        Process item using batch processor.

        Args:
            batch_key: Key to group items for batching
            item: Item to process
            processor_func: Function to process batch of items

        Returns:
            Processing result
        """
        if not self._batch_processor:
            # Fall back to individual processing
            return await processor_func([item])

        return await self._batch_processor.add_item(batch_key, item, processor_func)

    def get_performance_metrics(self) -> dict[str, Any]:
        """Get comprehensive performance metrics"""
        if not self._metrics:
            return {"error": "Metrics not enabled"}

        metrics = self._metrics.get_performance_summary()

        # Add component-specific metrics
        if self._connection_pool:
            metrics["connection_pool"] = self._connection_pool.get_stats()

        if self._batch_processor:
            metrics["batch_processor"] = self._batch_processor.get_stats()

        # Add cache metrics
        metrics["response_cache"] = {
            "size": len(self._response_cache),
            "timeout_seconds": self._cache_timeout
        }

        return metrics

    def optimize_for_latency(self):
        """Apply latency-focused optimizations"""
        logger.info("Applying latency optimizations")

        # Reduce batch size and timeout for lower latency
        if self._batch_processor:
            self._batch_processor.batch_size = min(10, self._batch_processor.batch_size)
            self._batch_processor.batch_timeout_ms = min(10, self._batch_processor.batch_timeout_ms)

        # Reduce cache timeout
        self._cache_timeout = 0.5

        # Update performance targets
        if self._metrics:
            self._metrics.targets["max_latency_ms"] = 5.0  # Stricter latency target

        logger.info("Latency optimizations applied")

    def optimize_for_throughput(self):
        """Apply throughput-focused optimizations"""
        logger.info("Applying throughput optimizations")

        # Increase batch size for higher throughput
        if self._batch_processor:
            self._batch_processor.batch_size = max(100, self._batch_processor.batch_size)
            self._batch_processor.batch_timeout_ms = max(50, self._batch_processor.batch_timeout_ms)

        # Increase cache timeout
        self._cache_timeout = 5.0

        # Update performance targets
        if self._metrics:
            self._metrics.targets["min_throughput_rps"] = 1000.0  # Higher throughput target

        logger.info("Throughput optimizations applied")

    def _serialize_payload(self, data: Any) -> bytes:
        """Serialize payload for NATS transmission"""
        import json
        return json.dumps(data).encode()

    def _deserialize_response(self, response_msg) -> Any:
        """Deserialize NATS response"""
        import json
        # Handle both Msg objects and bytes
        if hasattr(response_msg, 'data'):
            data = response_msg.data
        else:
            data = response_msg
        return json.loads(data.decode())

    async def _cleanup_cache(self):
        """Clean up expired cache entries"""
        current_time = time.time()
        expired_keys = [
            key for key, entry in self._response_cache.items()
            if current_time - entry["timestamp"] > self._cache_timeout
        ]

        for key in expired_keys:
            del self._response_cache[key]

        if expired_keys and self._metrics:
            self._metrics.increment_counter("cache_cleanups", len(expired_keys))

    async def health_check_optimized(self) -> dict[str, Any]:
        """Enhanced health check with performance data"""
        base_health = await super().health_check()

        if self._metrics:
            performance_checks = self._metrics.check_performance_targets()
            base_health["performance"] = performance_checks
            base_health["metrics_available"] = True
        else:
            base_health["metrics_available"] = False

        if self._connection_pool:
            base_health["connection_pool"] = self._connection_pool.get_stats()

        return base_health

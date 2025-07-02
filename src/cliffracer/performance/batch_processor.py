"""
High-performance batch processing for Cliffracer services
"""

import asyncio
import time
import weakref
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from loguru import logger


class BatchProcessor:
    """
    High-performance batch processor for aggregating and processing requests.

    This processor can significantly improve throughput by batching multiple
    requests together and processing them in bulk.
    """

    def __init__(
        self, batch_size: int = 100, batch_timeout_ms: int = 50, max_concurrent_batches: int = 10
    ):
        """
        Initialize batch processor.

        Args:
            batch_size: Maximum number of items per batch
            batch_timeout_ms: Maximum time to wait for batch to fill (milliseconds)
            max_concurrent_batches: Maximum number of concurrent batch processes
        """
        from ..core.validation import NumericBounds, validate_batch_size, validate_timeout

        # Validate inputs
        self.batch_size = validate_batch_size(batch_size)
        self.batch_timeout_ms = int(
            validate_timeout(batch_timeout_ms / 1000, min_ms=1, max_ms=60000) * 1000
        )

        if not isinstance(max_concurrent_batches, int) or max_concurrent_batches < 1:
            raise ValueError("max_concurrent_batches must be a positive integer")
        if max_concurrent_batches > NumericBounds.MAX_CONCURRENT:
            raise ValueError(f"max_concurrent_batches cannot exceed {NumericBounds.MAX_CONCURRENT}")

        self.max_concurrent_batches = max_concurrent_batches

        self._batches: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._batch_futures: dict[str, list[asyncio.Future]] = defaultdict(list)
        self._batch_timers: dict[str, asyncio.Task | None] = {}
        self._batch_tasks: set[asyncio.Task] = weakref.WeakSet()  # Track running tasks
        self._processing_lock = asyncio.Lock()
        self._concurrent_batches = 0
        self._shutdown = False

        # Statistics
        self.stats = {
            "total_items_processed": 0,
            "total_batches_processed": 0,
            "average_batch_size": 0,
            "processing_time_total_ms": 0,
            "items_per_second": 0,
        }

    async def add_item(
        self, batch_key: str, item: Any, processor: Callable[[list[Any]], Any]
    ) -> Any:
        """
        Add item to batch for processing.

        Args:
            batch_key: Key to group items into batches
            item: Item to process
            processor: Function to process the batch

        Returns:
            Result of processing this item
        """
        future = asyncio.Future()

        async with self._processing_lock:
            # Add item and future to batch
            self._batches[batch_key].append(
                {"item": item, "future": future, "processor": processor}
            )
            self._batch_futures[batch_key].append(future)

            # Check if batch is full
            if len(self._batches[batch_key]) >= self.batch_size:
                await self._process_batch(batch_key)
            elif batch_key not in self._batch_timers or self._batch_timers[batch_key] is None:
                # Start timeout timer if not already running
                self._batch_timers[batch_key] = asyncio.create_task(self._batch_timeout(batch_key))

        # Wait for result
        return await future

    async def _batch_timeout(self, batch_key: str):
        """Handle batch timeout"""
        await asyncio.sleep(self.batch_timeout_ms / 1000.0)

        async with self._processing_lock:
            if batch_key in self._batches and self._batches[batch_key]:
                await self._process_batch(batch_key)

    async def _process_batch(self, batch_key: str):
        """Process a complete batch"""
        if not self._batches[batch_key]:
            return

        # Wait if too many concurrent batches
        while self._concurrent_batches >= self.max_concurrent_batches:
            await asyncio.sleep(0.001)  # Small delay

        # Extract batch items
        batch_items = self._batches[batch_key]
        batch_futures = self._batch_futures[batch_key]

        # Clear the batch
        self._batches[batch_key] = []
        self._batch_futures[batch_key] = []

        # Cancel timeout timer
        if self._batch_timers.get(batch_key):
            self._batch_timers[batch_key].cancel()
            self._batch_timers[batch_key] = None

        # Process batch asynchronously with proper tracking
        if not self._shutdown:
            task = asyncio.create_task(self._execute_batch(batch_items, batch_futures))
            self._batch_tasks.add(task)
            # Clean up completed tasks periodically
            task.add_done_callback(lambda t: self._batch_tasks.discard(t))

    async def _execute_batch(
        self, batch_items: list[dict[str, Any]], futures: list[asyncio.Future]
    ):
        """Execute batch processing"""
        self._concurrent_batches += 1
        start_time = time.perf_counter()

        try:
            # Group items by processor
            processor_groups = defaultdict(list)
            future_mapping = {}

            for i, batch_item in enumerate(batch_items):
                processor = batch_item["processor"]
                item = batch_item["item"]
                future = futures[i]

                processor_id = id(processor)
                processor_groups[processor_id].append(item)

                if processor_id not in future_mapping:
                    future_mapping[processor_id] = {"processor": processor, "futures": []}
                future_mapping[processor_id]["futures"].append(future)

            # Process each group
            for processor_id, items in processor_groups.items():
                processor_info = future_mapping[processor_id]
                processor = processor_info["processor"]
                group_futures = processor_info["futures"]

                try:
                    # Process the batch
                    if asyncio.iscoroutinefunction(processor):
                        results = await processor(items)
                    else:
                        results = processor(items)

                    # Handle results
                    if isinstance(results, list) and len(results) == len(group_futures):
                        # Individual results for each item
                        for future, result in zip(group_futures, results, strict=False):
                            if not future.cancelled():
                                future.set_result(result)
                    else:
                        # Single result for all items
                        for future in group_futures:
                            if not future.cancelled():
                                future.set_result(results)

                except Exception as e:
                    logger.error(f"Batch processing error: {e}")
                    # Set exception for all futures in this group
                    for future in group_futures:
                        if not future.cancelled():
                            future.set_exception(e)

            # Update statistics
            end_time = time.perf_counter()
            processing_time_ms = (end_time - start_time) * 1000

            self.stats["total_items_processed"] += len(batch_items)
            self.stats["total_batches_processed"] += 1
            self.stats["processing_time_total_ms"] += processing_time_ms

            if self.stats["total_batches_processed"] > 0:
                self.stats["average_batch_size"] = (
                    self.stats["total_items_processed"] / self.stats["total_batches_processed"]
                )

            if self.stats["processing_time_total_ms"] > 0:
                self.stats["items_per_second"] = self.stats["total_items_processed"] / (
                    self.stats["processing_time_total_ms"] / 1000
                )

            logger.debug(
                f"Processed batch of {len(batch_items)} items in {processing_time_ms:.2f}ms"
            )

        finally:
            self._concurrent_batches -= 1

    async def flush_all(self):
        """Force process all pending batches"""
        async with self._processing_lock:
            for batch_key in list(self._batches.keys()):
                if self._batches[batch_key]:
                    await self._process_batch(batch_key)

    def get_stats(self) -> dict[str, Any]:
        """Get batch processor statistics"""
        stats = self.stats.copy()
        stats.update(
            {
                "pending_batches": len([b for b in self._batches.values() if b]),
                "concurrent_batches": self._concurrent_batches,
                "batch_size_limit": self.batch_size,
                "batch_timeout_ms": self.batch_timeout_ms,
                "max_concurrent_batches": self.max_concurrent_batches,
            }
        )
        return stats

    def reset_stats(self):
        """Reset all statistics"""
        self.stats = {
            "total_items_processed": 0,
            "total_batches_processed": 0,
            "average_batch_size": 0,
            "processing_time_total_ms": 0,
            "items_per_second": 0,
        }

    async def shutdown(self):
        """Gracefully shutdown the batch processor"""
        logger.info("Shutting down batch processor...")
        self._shutdown = True

        # Process any remaining batches
        await self.flush_all()

        # Cancel all timers
        for timer in self._batch_timers.values():
            if timer:
                timer.cancel()
        self._batch_timers.clear()

        # Wait for all running tasks to complete
        if self._batch_tasks:
            logger.info(f"Waiting for {len(self._batch_tasks)} batch tasks to complete...")
            await asyncio.gather(*self._batch_tasks, return_exceptions=True)

        # Clear all data structures
        self._batches.clear()
        self._batch_futures.clear()

        logger.info("Batch processor shutdown complete")

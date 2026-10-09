"""Batch aggregation and scheduled flush for message payloads."""

import asyncio
import time
import weakref
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Literal

from loguru import logger


def _processor_key(processor: Callable[[list[Any]], Any]) -> Any:
    """What makes two items' processors the same one, so they are called together.

    The processor itself: Python compares bound methods as the same owner and the
    same function, for built-in and Python methods alike, so `service.handle`
    named twice is one processor although every access builds a new object, and
    two objects' methods are two. Anything else is its own processor unless it
    defines equality, so two closures or partials are two processors even when
    they would do the same work. A callable that cannot be hashed is keyed by
    identity.
    """
    try:
        hash(processor)
    except TypeError:
        return id(processor)
    return processor


def _describe(value: Any) -> str:
    """What a processor returned, for the error that says it was the wrong shape."""
    if isinstance(value, list | tuple):
        return f"a {type(value).__name__} of {len(value)}"
    return f"a {type(value).__name__}"


class BatchProcessor:
    """Aggregates items into batches triggered by count or timeout."""

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
        from cliffracer.core.validation import NumericBounds, validate_batch_size, validate_timeout

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
        self._batch_tasks: weakref.WeakSet[asyncio.Task[Any]] = (
            weakref.WeakSet()
        )  # Track running tasks
        self._processing_lock = asyncio.Lock()
        self._concurrent_batches = 0
        # Wall-clock time at least one batch has been running, so overlapping batches are counted
        # once and the idle time between batches not at all: what `items_per_second` divides by.
        self._busy_seconds = 0.0
        self._busy_since: float | None = None
        self._shutdown = False

        # Statistics
        self.stats: dict[str, Any] = {
            "total_items_processed": 0,
            "total_batches_processed": 0,
            "average_batch_size": 0,
            "processing_time_total_ms": 0,
            "items_per_second": 0,
        }

    async def add_item(
        self,
        batch_key: str,
        item: Any,
        processor: Callable[[list[Any]], Any],
        *,
        results: Literal["shared", "per_item"] = "shared",
    ) -> Any:
        """
        Add item to batch for processing.

        Args:
            batch_key: Key to group items into batches
            item: Item to process
            processor: Function to process the batch. Items added with the same function,
                or the same method of the same object, and the same ``results``, are processed
                in one call.
            results: What the caller receives, said by the caller and never guessed from what
                the processor returned. ``"shared"`` (the default): the processor's return
                value, as it is, whatever its type. ``"per_item"``: this item's own result, the
                element of the processor's return value at this item's place in the call; the
                processor must return a list or tuple with one result for each of its items,
                and a batch whose processor returns anything else fails every one of its
                callers with a ``ValueError``.

        Returns:
            The result of processing this item, as ``results`` says
        """
        if results not in ("shared", "per_item"):
            raise ValueError(f"results must be 'shared' or 'per_item', got {results!r}")
        if self._shutdown:
            raise RuntimeError("BatchProcessor is shutting down")

        future: asyncio.Future[Any] = asyncio.Future()

        async with self._processing_lock:
            # Add item and future to batch
            self._batches[batch_key].append(
                {"item": item, "future": future, "processor": processor, "results": results}
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

    async def _batch_timeout(self, batch_key: str) -> None:
        """Handle batch timeout"""
        await asyncio.sleep(self.batch_timeout_ms / 1000.0)

        async with self._processing_lock:
            if batch_key in self._batches and self._batches[batch_key]:
                await self._process_batch(batch_key)

    async def _process_batch(self, batch_key: str) -> None:
        """Process a complete batch"""
        if not self._batches[batch_key]:
            return

        # Extract batch items
        batch_items = self._batches[batch_key]
        batch_futures = self._batch_futures[batch_key]

        # Clear the batch
        self._batches[batch_key] = []
        self._batch_futures[batch_key] = []

        # Cancel timeout timer
        timer = self._batch_timers.get(batch_key)
        if timer is not None:
            timer.cancel()
            self._batch_timers[batch_key] = None

        # Process batch asynchronously with proper tracking
        task = asyncio.create_task(self._execute_batch(batch_items, batch_futures))
        self._batch_tasks.add(task)
        # Clean up completed tasks periodically
        task.add_done_callback(lambda t: self._batch_tasks.discard(t))

    async def _execute_batch(
        self, batch_items: list[dict[str, Any]], futures: list[asyncio.Future[Any]]
    ) -> None:
        """Execute batch processing.

        A batch that is cancelled, or ends in anything that is not an ``Exception``, fails the
        callers it had not yet answered with a ``RuntimeError``: they are waiting on futures
        nothing else will resolve. A group the batch had already answered keeps its result.
        """
        counted = False
        try:
            while self._concurrent_batches >= self.max_concurrent_batches:
                await asyncio.sleep(0.001)

            if self._concurrent_batches == 0:
                self._busy_since = time.perf_counter()
            self._concurrent_batches += 1
            counted = True
            start_time = time.perf_counter()

            # Group items by processor
            processor_groups = defaultdict(list)
            future_mapping = {}

            for i, batch_item in enumerate(batch_items):
                processor = batch_item["processor"]
                item = batch_item["item"]
                future = futures[i]

                # One call per processor and per way of answering it: the same function asked
                # for both is two calls, because its return value is read two ways.
                processor_id = (_processor_key(processor), batch_item["results"])
                processor_groups[processor_id].append(item)

                if processor_id not in future_mapping:
                    future_mapping[processor_id] = {
                        "processor": processor,
                        "futures": [],
                        "results": batch_item["results"],
                    }
                future_mapping[processor_id]["futures"].append(future)

            # Process each group
            for processor_id, items in processor_groups.items():
                processor_info = future_mapping[processor_id]
                processor = processor_info["processor"]
                group_futures = processor_info["futures"]

                try:
                    # Process the batch
                    if asyncio.iscoroutinefunction(processor):
                        outcome = await processor(items)
                    else:
                        outcome = processor(items)

                    if processor_info["results"] == "per_item":
                        if not isinstance(outcome, list | tuple) or len(outcome) != len(
                            group_futures
                        ):
                            raise ValueError(
                                f"results='per_item' needs the processor to return a list or "
                                f"tuple with one result for each of its {len(group_futures)} "
                                f"items, got {_describe(outcome)}"
                            )
                        for future, result in zip(group_futures, outcome, strict=True):
                            if not future.cancelled():
                                future.set_result(result)
                    else:
                        for future in group_futures:
                            if not future.cancelled():
                                future.set_result(outcome)

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

            busy = self._busy_time()
            if busy > 0:
                self.stats["items_per_second"] = self.stats["total_items_processed"] / busy

            logger.debug(
                f"Processed batch of {len(batch_items)} items in {processing_time_ms:.2f}ms"
            )

        except BaseException as interruption:
            unanswered = [future for future in futures if not future.done()]
            for future in unanswered:
                future.set_exception(
                    RuntimeError(
                        f"the batch was interrupted ({type(interruption).__name__}) before this "
                        f"item's result was known; it may or may not have been processed"
                    )
                )
            if unanswered:
                logger.error(
                    f"Batch interrupted by {type(interruption).__name__}: "
                    f"{len(unanswered)} of {len(futures)} callers were failed"
                )
            raise
        finally:
            if counted:
                self._concurrent_batches -= 1
                if self._concurrent_batches == 0 and self._busy_since is not None:
                    self._busy_seconds += time.perf_counter() - self._busy_since
                    self._busy_since = None

    def _busy_time(self) -> float:
        """Seconds a batch has been running, the one in progress included."""
        if self._busy_since is None:
            return self._busy_seconds
        return self._busy_seconds + (time.perf_counter() - self._busy_since)

    async def flush_all(self) -> None:
        """Force process all pending batches"""
        async with self._processing_lock:
            for batch_key in list(self._batches.keys()):
                if self._batches[batch_key]:
                    await self._process_batch(batch_key)

    def get_stats(self) -> dict[str, Any]:
        """Get batch processor statistics.

        `items_per_second` is the items processed divided by the wall-clock time at least one batch
        was running: batches that overlap count once, and the idle time between batches not at all,
        so it is the rate the processor sustains while it is working, not a rate over the time the
        processor has existed. `processing_time_total_ms` is the sum of each batch's own duration,
        which counts overlapping batches twice.
        """
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

    def reset_stats(self) -> None:
        """Reset all statistics"""
        self._busy_seconds = 0.0
        if self._busy_since is not None:
            self._busy_since = time.perf_counter()
        self.stats = {
            "total_items_processed": 0,
            "total_batches_processed": 0,
            "average_batch_size": 0,
            "processing_time_total_ms": 0,
            "items_per_second": 0,
        }

    async def shutdown(self) -> None:
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
            await asyncio.gather(*list(self._batch_tasks), return_exceptions=True)

        # Clear all data structures
        self._batches.clear()
        self._batch_futures.clear()

        logger.info("Batch processor shutdown complete")

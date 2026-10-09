"""Items that name the same method of the same object are processed as one batch.

`BatchProcessor` groups the items of a batch by the processor each was added
with and calls each processor once. Every access to `service.handle` builds a
new bound-method object, and each item holds its own, so grouping by the
identity of that object delivered a batch of N items as N calls of one -- while
the statistics reported one batch of N. The natural way to pass a processor, as
a method, defeated batching without a sign.

A plain function is one object however often it is named, so it grouped
correctly, which is why the first test is the control.
"""

import asyncio

import pytest
from cliffracer_metrics import BatchProcessor

pytestmark = pytest.mark.unit

SIZE = 3


def _processor() -> BatchProcessor:
    return BatchProcessor(batch_size=SIZE, batch_timeout_ms=5000)


class Handler:
    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    async def handle(self, items: list[int]) -> list[int]:
        self.calls.append(list(items))
        return [item * 2 for item in items]


async def test_CONTROL_a_plain_function_processes_the_batch_in_one_call():
    calls: list[list[int]] = []

    async def process(items: list[int]) -> list[int]:
        calls.append(list(items))
        return list(items)

    batcher = _processor()
    await asyncio.gather(*(batcher.add_item("k", i, process) for i in range(SIZE)))

    assert calls == [[0, 1, 2]]
    await batcher.shutdown()


async def test_a_bound_method_processes_the_batch_in_one_call():
    handler = Handler()
    batcher = _processor()

    results = await asyncio.gather(
        *(batcher.add_item("k", i, handler.handle, results="per_item") for i in range(SIZE))
    )

    assert handler.calls == [[0, 1, 2]], "a batch of 3 was delivered as separate calls"
    assert results == [0, 2, 4]
    await batcher.shutdown()


async def test_the_methods_of_two_objects_are_not_merged_into_one_call():
    first, second = Handler(), Handler()
    batcher = _processor()

    await asyncio.gather(
        batcher.add_item("k", 0, first.handle),
        batcher.add_item("k", 1, second.handle),
        batcher.add_item("k", 2, first.handle),
    )

    assert first.calls == [[0, 2]]
    assert second.calls == [[1]]
    await batcher.shutdown()


async def test_a_bound_method_of_a_builtin_type_processes_the_batch_in_one_call():
    """`",".join` is a bound method with an owner but no `__func__`."""
    batcher = _processor()

    results = await asyncio.gather(*(batcher.add_item("k", c, ",".join) for c in "abc"))

    assert results == ["a,b,c", "a,b,c", "a,b,c"], "a built-in method split the batch"
    await batcher.shutdown()


async def test_a_method_of_an_unhashable_owner_still_batches():
    class Unhashable(Handler):
        __hash__ = None  # type: ignore[assignment]

        def __eq__(self, other):
            return self is other

    handler = Unhashable()
    batcher = _processor()

    await asyncio.gather(*(batcher.add_item("k", i, handler.handle) for i in range(SIZE)))

    assert handler.calls == [[0, 1, 2]]
    await batcher.shutdown()


async def test_an_unhashable_callable_object_still_batches_by_identity():
    calls: list[list[int]] = []

    class Processor:
        __hash__ = None  # type: ignore[assignment]

        def __eq__(self, other):
            return self is other

        def __call__(self, items: list[int]) -> list[int]:
            calls.append(list(items))
            return list(items)

    processor = Processor()
    batcher = _processor()

    await asyncio.gather(*(batcher.add_item("k", i, processor) for i in range(SIZE)))

    assert calls == [[0, 1, 2]]
    await batcher.shutdown()


async def test_a_new_closure_per_call_is_still_a_processor_of_its_own():
    """Two different closures are two processors, however alike. They are not the
    same callable, and nothing can tell that they would do the same work."""
    calls: list[list[int]] = []

    def make():
        async def process(items: list[int]) -> list[int]:
            calls.append(list(items))
            return list(items)

        return process

    batcher = _processor()
    await asyncio.gather(*(batcher.add_item("k", i, make()) for i in range(SIZE)))

    assert sorted(calls) == [[0], [1], [2]]
    await batcher.shutdown()

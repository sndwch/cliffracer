"""`add_item` takes what its caller receives from `results`, never from the shape of the return.

The processor's return value was split among the callers whenever it was a list as long as the batch
and handed whole otherwise, so a processor that returned one aggregate list was unpacked exactly when
the list happened to be as long as the batch: always for a batch of one, which is what a timeout
flush produces under light load. The caller says which it wants.
"""

import asyncio

import pytest
from cliffracer_metrics import BatchProcessor

pytestmark = pytest.mark.unit


def _processor(size: int) -> BatchProcessor:
    return BatchProcessor(batch_size=size, batch_timeout_ms=5000)


async def test_a_batch_of_one_whose_processor_returns_an_aggregate_list_gets_the_whole_list():
    """The coincidence the length rule turned into an element."""
    bp = _processor(1)

    result = await bp.add_item("k", "x", lambda items: ["aggregate-row"])

    assert result == ["aggregate-row"]


async def test_an_aggregate_list_as_long_as_the_batch_is_handed_whole_to_every_caller():
    bp = _processor(3)
    aggregate = ["row-a", "row-b", "row-c"]

    def processor(items):
        return aggregate

    results = await asyncio.gather(*(bp.add_item("k", i, processor) for i in range(3)))

    assert results == [aggregate, aggregate, aggregate]


async def test_shared_is_the_default_and_hands_a_non_list_result_over_as_it_is():
    bp = _processor(2)
    summary = {"written": 2}

    def processor(items):
        return summary

    results = await asyncio.gather(*(bp.add_item("k", i, processor) for i in range(2)))

    assert results[0] is summary and results[1] is summary


async def test_per_item_gives_each_caller_its_own_element():
    bp = _processor(3)

    def processor(items):
        return [item * 10 for item in items]

    results = await asyncio.gather(
        *(bp.add_item("k", i, processor, results="per_item") for i in range(3))
    )

    assert results == [0, 10, 20]


async def test_per_item_gives_a_batch_of_one_its_element_not_the_list():
    bp = _processor(1)

    result = await bp.add_item("k", 7, lambda items: [70], results="per_item")

    assert result == 70


async def test_per_item_accepts_a_tuple():
    bp = _processor(2)

    def processor(items):
        return tuple(item + 1 for item in items)

    results = await asyncio.gather(
        *(bp.add_item("k", i, processor, results="per_item") for i in (1, 2))
    )

    assert results == [2, 3]


@pytest.mark.parametrize(
    ("returned", "described"),
    [([1], "a list of 1"), ([1, 2, 3], "a list of 3"), ("ab", "a str"), (None, "a NoneType")],
)
async def test_per_item_fails_every_caller_when_the_return_is_not_one_result_per_item(
    returned, described
):
    bp = _processor(2)

    def processor(items):
        return returned

    outcomes = await asyncio.gather(
        *(bp.add_item("k", i, processor, results="per_item") for i in range(2)),
        return_exceptions=True,
    )

    assert all(isinstance(o, ValueError) for o in outcomes), outcomes
    assert all(
        "results='per_item' needs" in str(o) and "each of its 2 items" in str(o) for o in outcomes
    )
    assert all(described in str(o) for o in outcomes)


async def test_one_processor_asked_both_ways_is_called_once_for_each_way():
    calls: list[list[int]] = []
    bp = _processor(4)

    def processor(items):
        calls.append(list(items))
        return [item * 2 for item in items]

    outcomes = await asyncio.gather(
        bp.add_item("k", 1, processor, results="per_item"),
        bp.add_item("k", 2, processor),
        bp.add_item("k", 3, processor, results="per_item"),
        bp.add_item("k", 4, processor),
    )

    assert sorted(calls) == [[1, 3], [2, 4]]
    assert outcomes == [2, [4, 8], 6, [4, 8]]


async def test_an_unknown_results_value_is_refused_before_anything_is_queued():
    bp = _processor(1)

    with pytest.raises(ValueError, match="results must be 'shared' or 'per_item', got 'each'"):
        await bp.add_item("k", 1, lambda items: items, results="each")  # type: ignore[arg-type]

    assert bp.get_stats()["pending_batches"] == 0

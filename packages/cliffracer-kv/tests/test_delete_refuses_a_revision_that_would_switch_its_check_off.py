"""`delete(last=n)` checks the revision it was given, or refuses it.

nats-py applies the compare-and-delete only `if last and last > 0`, so `delete(last=0)` and
`delete(last=-1)` deleted unconditionally: a caller whose stored revision defaulted to 0 deleted
without the check it asked for. `get(revision=)` already refused a revision that is not a positive
integer; `delete` now does the same, before it touches the bucket.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from cliffracer_kv import KvExtension

pytestmark = pytest.mark.unit


def _extension() -> tuple[KvExtension, AsyncMock, AsyncMock]:
    kv = AsyncMock()
    js = AsyncMock()
    js.key_value.return_value = kv
    return KvExtension(buckets=["b"], js=js), kv, js


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [0, -1, True, False, "3", 1.5], ids=repr)
async def test_a_revision_that_is_not_a_positive_integer_is_refused_and_nothing_is_deleted(
    revision,
):
    extension, kv, js = _extension()

    with pytest.raises(ValueError, match="positive integer"):
        await extension.delete("b", "k", last=revision)

    kv.delete.assert_not_awaited()
    js.key_value.assert_not_awaited()


@pytest.mark.asyncio
async def test_CONTROL_a_positive_revision_is_passed_to_the_compare_and_delete():
    extension, kv, _ = _extension()

    await extension.delete("b", "k", last=7)

    kv.delete.assert_awaited_once_with("k", last=7)


@pytest.mark.asyncio
async def test_CONTROL_no_revision_deletes_unconditionally_as_before():
    extension, kv, _ = _extension()

    await extension.delete("b", "k")

    kv.delete.assert_awaited_once_with("k", last=None)

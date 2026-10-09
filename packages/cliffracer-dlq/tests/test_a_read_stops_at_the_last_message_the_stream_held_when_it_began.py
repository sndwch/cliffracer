"""A read stops at the last message the stream held when the read began.

A dead-letter stream keeps receiving while it is read. The read takes the stream's last sequence
from `stream_info` before it starts and stops there, so a listing ends even when messages keep
arriving behind it.
"""

import datetime
import json
from types import SimpleNamespace

import pytest
from cliffracer_dlq.reader import read
from nats.js.api import RawStreamMsg

pytestmark = pytest.mark.unit

WHEN = datetime.datetime(2026, 10, 2, 16, 0, 0, tzinfo=datetime.UTC)
RECORD = {"errors": [], "service": "orders"}


class GrowingStream:
    """A stream that held sequences 1 and 2 when asked, and has a next message at any sequence."""

    async def stream_info(self, name):
        return SimpleNamespace(state=SimpleNamespace(messages=2, first_seq=1, last_seq=2))

    async def get_msg(self, stream, seq=None, subject=None, direct=False, next=False):
        return RawStreamMsg(
            subject="dlq.orders", seq=seq, data=json.dumps(RECORD).encode(), headers={}, time=WHEN
        )


@pytest.mark.timeout(10)
async def test_a_read_of_a_stream_that_keeps_growing_ends_at_its_last_sequence():
    read_so_far = []
    async for dead_letter in read(GrowingStream(), "DLQ", "dlq.*"):
        read_so_far.append(dead_letter.sequence)
        if len(read_so_far) == 5:
            break

    assert read_so_far == [1, 2]

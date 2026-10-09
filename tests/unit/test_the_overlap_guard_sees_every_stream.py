"""The overlap guard reads every stream, not the first page of them.

`ensure_streams` refuses a declaration whose subjects collide with a stream
already on the broker, and `_assert_no_overlap`'s docstring says why it bothers:

    The server rejects this too, with "10054 subjects overlap with an existing
    stream" and no indication of which stream or which subject. Catching it
    here is the difference between a fixable error and a 3am one.

It built its picture from one `await js.streams_info()`. In nats-py that is a
single request -- `streams_info(offset=0)` issues one `STREAM.LIST` and returns
that page, with no loop -- and JetStream answers at most 256 per page. Past 256
streams the guard reported "no overlap" from a listing that did not contain the
stream it would have collided with, and the raw `BadRequestError` escaped from
`add_stream`.

MEASURED on the broker this was found on: `streams_info()` returned 256 of 406
streams, so 150 were invisible to the check. The symptom was
`tests/integration/test_jetstream_durable.py::test_a_conflicting_stream_declaration_fails_startup_with_a_named_error`
failing 7 runs in 20 -- intermittent because the stream it collides with is
created and deleted each run, so whether it lands inside page 0 moves.

THE FAKE ANSWERS IN PAGES ON PURPOSE. Three hundred real streams on a shared
broker is the condition that caused this; reproducing it by creating them is
the wrong direction. The fake's contract is the one nats-py has: an offset in,
at most `STREAM_LIST_PAGE` out.
"""

from __future__ import annotations

from typing import Any

import pytest

from cliffracer import StreamSpec
from cliffracer.core.jetstream import (
    StreamDeclarationError,
    all_streams,
    ensure_streams,
)

#: What a real JetStream page holds. Not imported from the module under test any
#: more: `all_streams` reads the server's own `total` and has no page-size
#: constant, so a test asserting against one would be asserting against a number
#: the code no longer uses.
PAGE = 256

pytestmark = pytest.mark.unit


class _Config:
    def __init__(self, name: str, subjects: list[str]) -> None:
        self.name = name
        self.subjects = subjects


class _Info:
    def __init__(self, name: str, subjects: list[str]) -> None:
        self.config = _Config(name, subjects)


class Page:
    """What `streams_info_iterator` returns: a `total`, and entries on iteration.

    nats-py's `StreamsListIterator` holds the raw dicts and converts each to a
    `StreamInfo` in `__next__`, so a caller reads `.config` off the iteration and
    `.total` off the object. This answers that shape.
    """

    def __init__(self, entries: list[_Info], total: int) -> None:
        self._entries = entries
        self.total = total

    def __iter__(self):
        return iter(self._entries)


class FakeJetStream:
    """A JetStream manager that answers the way nats-py does.

    One page per call from the given offset, and the server's own `total`.
    `add_stream` records rather than acts, so a test can see whether the guard
    let a declaration through.

    Defined here, beside the reader's own tests, and imported by
    `test_the_session_sweep_reaches_a_broker.py`. Two fakes for one interface
    drift apart the same way the two paging loops did, which is what this whole
    change is about.
    """

    def __init__(self, streams: list[_Info], page: int = PAGE, total: int | None = None) -> None:
        self.streams = streams
        self.page = page
        self.declared_total = len(streams) if total is None else total
        self.added: list[Any] = []
        self.offsets: list[int] = []

    async def streams_info_iterator(self, offset: int = 0) -> Page:
        self.offsets.append(offset)
        return Page(self.streams[offset : offset + self.page], self.declared_total)

    async def streams_info(self, offset: int = 0) -> list[_Info]:
        """Still answered, because `test_CONTROL_a_single_page_reader_misses_it`
        reproduces the ORIGINAL single-page read against this same fake."""
        return self.streams[offset : offset + self.page]

    async def add_stream(self, config: Any = None, **kwargs: Any) -> None:
        self.added.append(config)

    async def update_stream(self, config: Any = None, **kwargs: Any) -> None:  # pragma: no cover
        self.added.append(config)


def _crowd(count: int, *, last: _Info | None = None) -> list[_Info]:
    """`count` unrelated streams, optionally with a specific one at the end."""
    filler = [_Info(f"FILLER_{i:04}", [f"filler.{i}.>"]) for i in range(count)]
    return filler + ([last] if last else [])


async def test_a_stream_past_the_first_page_is_still_seen() -> None:
    """The defect: the colliding stream sits at position 300 and is invisible."""
    colliding = _Info("ALREADY_HERE", ["itest.events.*"])
    js = FakeJetStream(_crowd(300, last=colliding))
    spec = StreamSpec(name="MINE", subjects=["itest.events.>"])

    with pytest.raises(StreamDeclarationError) as exc:
        await ensure_streams(js, [spec])

    assert "ALREADY_HERE" in str(exc.value), exc.value
    assert "itest.events.>" in str(exc.value), exc.value
    assert js.added == [], "the guard must refuse BEFORE add_stream reaches the server"


async def test_CONTROL_a_single_page_reader_misses_it() -> None:
    """The bug, reproduced against the same fake, so the fixture is not the fix.

    Reading one page is what the code did. If this passed, the test above would
    be passing for some reason other than the paging.
    """
    colliding = _Info("ALREADY_HERE", ["itest.events.*"])
    js = FakeJetStream(_crowd(300, last=colliding))

    one_page = await js.streams_info()

    assert len(one_page) == PAGE
    assert not any(i.config.name == "ALREADY_HERE" for i in one_page), (
        "the colliding stream must be outside page 0, or this test proves nothing"
    )


async def test_the_reader_walks_every_page() -> None:
    """301 streams is two pages: offsets 0 and 256, and nothing after."""
    js = FakeJetStream(_crowd(301))

    infos = await all_streams(js)

    assert len(infos) == 301
    assert js.offsets == [0, PAGE]


async def test_CONTROL_an_exact_multiple_does_not_cost_an_extra_request() -> None:
    """A full page used to be ambiguous, so the walk spent a request finding out
    the listing was over. The server's `total` answers it: 256 of 256 is done."""
    js = FakeJetStream(_crowd(PAGE))

    infos = await all_streams(js)

    assert len(infos) == PAGE
    assert js.offsets == [0]


async def test_CONTROL_a_short_first_page_asks_once_when_that_is_everything() -> None:
    """The ordinary small broker: three of three, one request."""
    js = FakeJetStream(_crowd(3))

    infos = await all_streams(js)

    assert len(infos) == 3
    assert js.offsets == [0]


async def test_a_short_page_that_is_not_everything_does_not_end_the_walk() -> None:
    """The property the page-size rule could not express.

    A server answering fewer than it could -- a lower configured limit, another
    build -- ended the walk, because a short page WAS the termination condition.
    `total` makes a shortfall a reason to ask again.
    """
    js = FakeJetStream(_crowd(420), page=200)

    infos = await all_streams(js)

    assert len(infos) == 420, f"the walk stopped at {len(infos)} of 420"
    assert js.offsets == [0, 200, 400]


async def test_CONTROL_an_empty_broker_is_not_an_error() -> None:
    js = FakeJetStream([])

    assert await all_streams(js) == []


async def test_a_listing_that_never_ends_is_refused_rather_than_passed() -> None:
    """ "I could not check" must not return the same thing as "I checked".

    A server that answers a full page at every offset would otherwise loop
    forever, or -- with a bounded loop that returned what it had -- report a
    clean check from a partial listing, which is the defect this fixes wearing
    a different hat.
    """

    class NeverEnds(FakeJetStream):
        async def streams_info_iterator(self, offset: int = 0) -> Page:
            self.offsets.append(offset)
            entries = [_Info(f"S_{offset}_{i}", [f"s.{offset}.{i}"]) for i in range(self.page)]
            return Page(entries, total=10**9)

    js = NeverEnds([])

    with pytest.raises(StreamDeclarationError) as exc:
        await all_streams(js)

    assert "could not finish" in str(exc.value) or "cannot finish" in str(exc.value), exc.value


async def test_CONTROL_a_declaration_with_no_collision_still_goes_through() -> None:
    """Otherwise "refuses everything" would satisfy the first test."""
    js = FakeJetStream(_crowd(300, last=_Info("ELSEWHERE", ["other.events.*"])))
    spec = StreamSpec(name="MINE", subjects=["itest.events.>"])

    await ensure_streams(js, [spec])

    assert len(js.added) == 1

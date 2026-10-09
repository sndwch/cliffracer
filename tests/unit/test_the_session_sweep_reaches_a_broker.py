"""A session that isolated itself sweeps the broker it actually used.

`_broker_namespace` gives a session its own prefix and deletes everything under
it afterwards -- but it read the sweep address from `$CLIFFRACER_TEST_NATS_URL`
alone and returned silently when that was unset. A plain local `pytest` does not
set it; CI does, and CI discards its broker regardless. So the compensating
sweep never ran anywhere a leaked stream could survive.

That gap is half of why 362 streams accumulated on the shared broker from one
unit module across 181 runs: the module's own teardown deleted names the broker
never held, and the sweep that would have caught it was switched off in exactly
the configuration where it was needed. Both halves had to be true, which is why
each looked like the other one covered it.

THE DECISION IS A FUNCTION, so this can assert it directly rather than through a
pytest session -- the same reason `decided_prefix` exists, and its docstring
says so.
"""

from __future__ import annotations

import pytest

from cliffracer.core.jetstream import StreamDeclarationError, all_streams
from tests.broker_isolation import sweep_url
from tests.conftest import broker_url

pytestmark = pytest.mark.unit


def test_CONTROL_the_sweep_resolves_an_address_with_no_env_var(monkeypatch):
    """The case that was silently skipped: a plain local run."""
    monkeypatch.delenv("CLIFFRACER_TEST_NATS_URL", raising=False)

    resolved = sweep_url()

    assert resolved, "a session with no env var resolved no broker to sweep"
    assert resolved == broker_url(), resolved


def test_an_operators_address_still_wins(monkeypatch):
    """Naming a broker explicitly must keep pointing the sweep at it."""
    monkeypatch.setenv("CLIFFRACER_TEST_NATS_URL", "nats://elsewhere:4299")

    assert sweep_url() == "nats://elsewhere:4299"


def test_CONTROL_the_two_cases_are_different_addresses(monkeypatch):
    """Otherwise both tests above would pass on a function returning one constant."""
    monkeypatch.setenv("CLIFFRACER_TEST_NATS_URL", "nats://elsewhere:4299")
    named = sweep_url()
    monkeypatch.delenv("CLIFFRACER_TEST_NATS_URL", raising=False)
    defaulted = sweep_url()

    assert named != defaulted, (named, defaulted)


# --- the sweep must see the whole broker ------------------------------------
#
# The sweep and `ensure_streams` ask the same question -- what is on this broker
# -- and each had its own loop with its own termination rule. One read until an
# empty page; the other stopped at a page shorter than a hard-coded 256. Two
# rules for one question is how one of them stays wrong, so there is now one
# reader, in `cliffracer.core.jetstream`, and this file imports it.


# The fake lives with the reader's own tests, and is imported rather than
# copied: two fakes for one interface drift apart the same way the two paging
# loops did.
from tests.unit.test_the_overlap_guard_sees_every_stream import (  # noqa: E402
    FakeJetStream,
    Page,
)


def _named(count: int) -> list:
    from tests.unit.test_the_overlap_guard_sees_every_stream import _Info

    return [_Info(f"t0_m0_S{i}", [f"s.{i}.>"]) for i in range(count)]


async def test_the_sweep_sees_past_the_first_page():
    """Measured on the shared broker: `streams_info()` returned 256 of 436.

    A sweep that reads one page is blind to 41% of that broker, and a stream it
    cannot see is a stream it cannot delete -- so the cleanup would have been
    useless on exactly the broker whose size made the leak visible.
    """
    js = FakeJetStream(_named(420))

    seen = [i.config.name for i in await all_streams(js)]

    assert len(seen) == 420, len(seen)
    assert len(js.offsets) > 1, "one call cannot have read 420 names from a 256-wide page"


async def test_a_short_first_page_does_not_end_the_walk():
    """The property the page-size rule could not have.

    A server that answers fewer than it could -- a different build, a lower
    configured limit -- used to end the walk, because a short page WAS the
    termination condition. `total` is the server's own count, so a shortfall is
    now a reason to ask again rather than a reason to stop.
    """
    js = FakeJetStream(_named(420), page=200)

    seen = await all_streams(js)

    assert len(seen) == 420, f"the walk stopped at {len(seen)} of 420"
    assert js.offsets == [0, 200, 400], js.offsets


async def test_CONTROL_a_broker_that_fits_in_one_page_is_read_once():
    """The ordinary case must not pay for the fix.

    One call, not two: the server says there are three and hands over three, so
    there is nothing left to ask. The reader this replaces spent a second
    request discovering the listing was over.
    """
    js = FakeJetStream(_named(3))

    seen = await all_streams(js)

    assert [i.config.name for i in seen] == [f"t0_m0_S{i}" for i in range(3)]
    assert len(js.offsets) == 1, js.offsets


async def test_CONTROL_an_empty_broker_is_read_once_and_is_not_an_error():
    js = FakeJetStream([])

    assert await all_streams(js) == []
    assert len(js.offsets) == 1


async def test_a_broker_that_reports_more_than_it_will_hand_over_is_refused():
    """ "I could not finish" must not return what "I read it all" returns."""
    js = FakeJetStream(_named(10), total=99)

    with pytest.raises(StreamDeclarationError) as exc:
        await all_streams(js)

    assert "99" in str(exc.value), exc.value
    assert "cannot be finished" in str(exc.value), exc.value


async def test_CONTROL_a_listing_that_shrinks_under_the_walk_still_finishes():
    """A shared broker loses streams mid-walk, and that must not be an error.

    Another session deleting a stream between two pages lowers the total the
    second page reports. The walk must read that as done rather than as a
    server withholding entries -- otherwise a service refuses to start because
    a neighbour tidied up.
    """

    class Shrinking(FakeJetStream):
        async def streams_info_iterator(self, offset: int = 0) -> Page:
            page = await super().streams_info_iterator(offset=offset)
            if len(self.offsets) > 1:  # a neighbour deleted two mid-walk
                page.total -= 2
                page._entries = page._entries[:-2]
            return page

    js = Shrinking(_named(300))

    seen = await all_streams(js)

    assert len(seen) == 298, len(seen)

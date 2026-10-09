"""The record that lets a caller say WHY a message was not delivered.

A soak counts a message as lost when nothing completed it. Cyanide makes that
happen on purpose: `raise_after_delay` is refused before the handler runs, so a
listener whose whole body records completion records nothing, and the message
looks exactly like a leak. Measured on the real pipeline at the weights
load-testing/chaos_soak.py uses: 111 of 2000 listener dispatches, 5.55%.

Without a record the caller has only a log line, which it cannot query, so
every one of those is reported as an unexplained failure and the soak fails
every run. With one it can join and say "injected, expected".

WHAT IS ASSERTED HERE, AND WHY IT IS NOT THE RECORD ITSELF. Every count below
comes from the OBSERVED side -- a dispatch that raised, a reply that was
suppressed, a call that took the slow delay -- and is then compared to the
record. Asserting that the record has the length the weights predict would be
asserting arithmetic; asserting it against what the faults actually did is what
catches a record written twice, written for a mode that did not run, or not
written at all.
"""

from __future__ import annotations

import asyncio

import pytest
from cliffracer_cyanide import CyanideConfig, CyanideExtension, Injection
from pydantic import ValidationError

from cliffracer.core.correlation_extension import CorrelationExtension
from cliffracer.core.dispatch.pipeline import ExtensionPipeline
from cliffracer.core.extension import RejectMessage, WorkerContext

pytestmark = pytest.mark.unit

# Distinct so an observer can tell the two sleeping modes apart by how long the
# dispatch took. Far enough apart that ordinary jitter cannot swap them.
SLOW_DELAY = 0.02
SLEEP_DURATION = 0.12
SLOW_FLOOR = 0.01
SLEEP_FLOOR = 0.08


def build(**overrides):
    """A cyanide extension behind a correlation extension, as a service binds them.

    `CliffracerService._collect_extensions` binds `CorrelationExtension` before
    any declared extension, so `ctx.correlation_id` is always set by the time
    cyanide reads it. That ordering is what makes the record joinable, and it
    is reproduced here rather than assumed.
    """
    settings = {
        "enabled": True,
        "mode": "random",
        "seed": "record-seed",
        "slow_weight": 0.05,
        "drop_reply_weight": 0.05,
        "raise_after_delay_weight": 0.05,
        "sleep_past_timeout_weight": 0.05,
        "slow_delay": SLOW_DELAY,
        "raise_delay": 0.0,
        "sleep_timeout_duration": SLEEP_DURATION,
    }
    settings.update(overrides)
    config = CyanideConfig(**settings)
    ext = CyanideExtension(config=config)
    ext.name = "cyanide"
    ext.active_mode = config.mode
    correlation = CorrelationExtension()
    correlation.name = "_correlation"
    return ext, ExtensionPipeline([correlation, ext])


def context(item_id: str) -> WorkerContext:
    return WorkerContext(
        kind="listener",
        subject="chaos.event",
        headers={},
        correlation_id=None,
        payload={"item_id": item_id, "correlation_id": item_id},
        data={"handler_name": "process_event"},
    )


async def observe(pipeline: ExtensionPipeline, item_id: str) -> str | None:
    """Dispatch one message and return the fault it VISIBLY suffered."""
    ctx = context(item_id)

    async def call() -> None:
        return None

    started = asyncio.get_running_loop().time()
    try:
        await pipeline.run_worker(ctx, call)
    except RejectMessage:
        return "raise_after_delay"
    elapsed = asyncio.get_running_loop().time() - started
    if ctx.data.get("_cyanide_dropped_reply"):
        return "drop_reply"
    if elapsed >= SLEEP_FLOOR:
        return "sleep_past_timeout"
    if elapsed >= SLOW_FLOOR:
        return "slow"
    return None


async def test_the_record_matches_what_the_faults_actually_did():
    """Every injection the dispatches visibly suffered, and no others."""
    ext, pipeline = build()
    observed = [await observe(pipeline, f"msg_{i}") for i in range(1, 301)]
    seen = [mode for mode in observed if mode is not None]

    assert seen, "no fault was injected at all, so this asserts nothing"
    assert len(set(seen)) == 4, f"a mode never fired, so it is untested here: {set(seen)}"

    recorded = [entry.mode for entry in ext.injections()]
    assert recorded == seen, (
        "the record disagrees with what the dispatches did.\n"
        f"  observed: {len(seen)} {sorted(set(seen))}\n"
        f"  recorded: {len(recorded)} {sorted(set(recorded))}"
    )


async def test_a_raising_fault_is_recorded_even_though_it_raises():
    """Record before execute, named on its own so it cannot be lost quietly.

    `raise_after_delay` never returns, so a record written after the fault
    would not exist for the single mode a caller most needs to attribute. The
    300-dispatch comparison above covers this, but it covers four modes at
    once: simplify that test and this coverage goes with it without anything
    going red.
    """
    ext, pipeline = build(
        slow_weight=0.0,
        drop_reply_weight=0.0,
        raise_after_delay_weight=1.0,
        sleep_past_timeout_weight=0.0,
    )

    async def call() -> None:  # pragma: no cover - must never run
        raise AssertionError("the handler ran despite a refusing fault")

    with pytest.raises(RejectMessage):
        await pipeline.run_worker(context("msg_1"), call)

    assert [entry.mode for entry in ext.injections()] == ["raise_after_delay"]


@pytest.mark.parametrize("limit", [-1, 0])
def test_a_limit_below_one_is_refused_by_name(limit: int):
    """Both traps, refused at the field rather than inside the deque.

    -1 reached `deque(maxlen=...)` and died with "maxlen must be non-negative",
    which names the implementation and not the setting. 0 was accepted and was
    worse: it records nothing while counting every injection as dropped, so the
    record reads as permanently incomplete and can never attribute anything --
    a bounded record that never records is the false-failure shape this whole
    file exists to remove.
    """
    with pytest.raises(ValidationError) as raised:
        CyanideConfig(injection_record_limit=limit)

    assert "injection_record_limit" in str(raised.value)
    assert "greater than or equal to 1" in str(raised.value)


def test_CONTROL_a_limit_of_one_is_accepted():
    """The near miss: the floor is 1, not 2, and it has to be reachable."""
    assert CyanideConfig(injection_record_limit=1).injection_record_limit == 1


async def test_the_record_carries_the_correlation_id_a_caller_supplied():
    """The join key. A caller can only join on an id it chose itself.

    `CorrelationExtension` takes the id from a header, else from a
    `correlation_id` in the payload, else generates one. A generated id is
    known only to the service, so a caller that supplies neither gets a record
    it cannot match against anything -- which is the failure this test exists
    to keep visible.
    """
    ext, pipeline = build()
    for index in range(1, 301):
        await observe(pipeline, f"msg_{index}")

    entries = ext.injections()
    assert entries, "nothing was recorded"
    assert all(entry.correlation_id is not None for entry in entries)
    assert all(entry.correlation_id.startswith("msg_") for entry in entries), (
        f"the record did not keep the supplied id: {entries[:3]}"
    )
    assert all(entry.subject == "chaos.event" for entry in entries)
    assert all(entry.seed == "record-seed" for entry in entries)


async def test_CONTROL_a_generated_correlation_id_is_recorded_as_it_is():
    """Without a supplied id the record still holds one -- the service's own.

    Stated so the previous test is read correctly: the record is never empty of
    a correlation id, so "the record has ids" is not evidence a caller can join
    on them. The ids here are the service's and match nothing the caller knows.
    """
    ext, pipeline = build()
    for index in range(1, 201):
        ctx = WorkerContext(
            kind="listener",
            subject="chaos.event",
            headers={},
            correlation_id=None,
            payload={"item_id": f"msg_{index}"},  # no correlation_id supplied
            data={"handler_name": "process_event"},
        )

        async def call() -> None:
            return None

        try:
            await pipeline.run_worker(ctx, call)
        except RejectMessage:
            pass

    entries = ext.injections()
    assert entries, "nothing was recorded"
    assert all(entry.correlation_id is not None for entry in entries)
    assert not any(entry.correlation_id.startswith("msg_") for entry in entries), (
        "a caller id appeared without being supplied"
    )


async def test_no_weights_records_nothing():
    """The control for every count above: with nothing to inject, nothing is recorded."""
    ext, pipeline = build(
        slow_weight=0.0,
        drop_reply_weight=0.0,
        raise_after_delay_weight=0.0,
        sleep_past_timeout_weight=0.0,
    )
    observed = [await observe(pipeline, f"msg_{i}") for i in range(1, 301)]

    assert observed == [None] * 300, "a fault fired with every weight at zero"
    assert ext.injections() == []
    assert ext.injections_dropped == 0


async def test_disabled_records_nothing():
    """Disabled is not the same as unweighted, and neither may record."""
    ext, pipeline = build(enabled=False)
    for index in range(1, 101):
        await observe(pipeline, f"msg_{index}")
    assert ext.injections() == []


async def test_the_record_survives_clearing_the_mode():
    """`set_mode(None)` stops further injection; it does not erase the history."""
    ext, pipeline = build()
    for index in range(1, 301):
        await observe(pipeline, f"msg_{index}")
    before = ext.injections()
    assert before, "nothing was recorded before the mode was cleared"

    ext.set_mode(None)
    after_clearing = [await observe(pipeline, f"later_{i}") for i in range(1, 101)]

    assert after_clearing == [None] * 100, "a fault fired after the mode was cleared"
    assert ext.injections() == before, "clearing the mode changed the record"


async def test_the_record_is_bounded_and_says_what_it_dropped():
    """Oldest first, and the drop count is what stops a gap reading as an absence.

    A caller joins "this message was not delivered" against the record. If the
    bound drops entries silently, a missing entry means either "not injected"
    or "no longer remembered", and the caller reports an unexplained failure
    for a message that cyanide faulted on purpose. The count is what lets it
    refuse to conclude instead.
    """
    ext, pipeline = build(injection_record_limit=8)
    index = 0
    while len(ext.injections()) < 8:
        index += 1
        await observe(pipeline, f"msg_{index}")
        assert index < 1200, "the record never filled"

    assert ext.injections_dropped == 0, "dropped before the bound was reached"
    full = ext.injections()
    oldest = full[0]

    while ext.injections_dropped == 0:
        index += 1
        await observe(pipeline, f"msg_{index}")
        assert index < 1200, "the bound never dropped anything"

    assert len(ext.injections()) == 8, "the record grew past its bound"
    assert oldest not in ext.injections(), "the bound dropped something other than the oldest"
    assert ext.injections()[0] == full[1], "the surviving entries are not the newest"


async def test_draining_empties_the_record_but_keeps_the_drop_count():
    """A caller that drains per interval still needs to know it missed something."""
    ext, pipeline = build(injection_record_limit=4)
    index = 0
    while ext.injections_dropped == 0:
        index += 1
        await observe(pipeline, f"msg_{index}")
        assert index < 1200, "the bound never dropped anything"

    dropped = ext.injections_dropped
    drained = ext.drain_injections()

    assert drained, "drain returned nothing"
    assert all(isinstance(entry, Injection) for entry in drained)
    assert ext.injections() == [], "drain left the record populated"
    assert ext.injections_dropped == dropped, "drain reset the drop count"


async def test_each_service_instance_gets_its_own_record():
    """Two services must not share one record.

    An extension is declared as a class attribute and `create_instance`
    rebuilds it per service through `__init__`, so the deque is per instance.
    That is easy to break by moving the record to a class attribute, and the
    breakage is quiet: the counts stay plausible and the entries of two
    services interleave, so a caller joins one service's ledger against
    another's faults.
    """
    declaration, _ = build()
    first = declaration.create_instance(None, "cyanide")
    second = declaration.create_instance(None, "cyanide")

    assert first.injections() == [] and second.injections() == []

    first._record(
        WorkerContext(
            kind="listener",
            subject="a.subject",
            headers={},
            correlation_id="only-the-first",
            payload={},
        ),
        "slow",
    )

    assert len(first.injections()) == 1
    assert second.injections() == [], "the record is shared between service instances"
    assert declaration.injections() == [], "a record landed on the declaration"


async def test_health_reports_the_counts_and_the_bound():
    """Counts, not the record: a health payload is read on a schedule."""
    ext, pipeline = build(injection_record_limit=4)
    index = 0
    while ext.injections_dropped == 0:
        index += 1
        await observe(pipeline, f"msg_{index}")
        assert index < 1200, "the bound never dropped anything"

    details = ext.health_details()
    assert details["injection_record_limit"] == 4
    assert details["injections_recorded"] == len(ext.injections()) == 4
    assert details["injections_dropped"] == ext.injections_dropped > 0

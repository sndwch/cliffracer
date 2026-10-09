"""The soak must tell a fault it injected from a message it lost.

Cyanide refuses a message BEFORE its handler runs. Both soak listeners record
delivery from inside the handler body, so a refused dispatch leaves exactly
what a lost one leaves: a send with nothing against it. Measured on the real
pipeline at the weights `load-testing/chaos_soak.py` configures -- 111 of 2000
listener dispatches, 5.55% -- and with no way to tell the two apart the soak
reports every one as a leak and fails every run.

`classify` is the join. These tests drive it directly, with fixtures rather
than a broker, because the question is what the verdict concludes from a given
ledger and not whether NATS delivers.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def _load_soak():
    """Import the harness, which lives outside the package tree."""
    path = REPO / "load-testing" / "chaos_soak.py"
    spec = importlib.util.spec_from_file_location("chaos_soak", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["chaos_soak"] = module
    spec.loader.exec_module(module)
    return module


soak = _load_soak()


@dataclass(frozen=True)
class FakeInjection:
    """The shape `CyanideExtension.injections()` returns.

    Local so these tests state what they depend on: a correlation id and a
    mode. If the real record loses either, the soak cannot join, and the
    coupling should be visible here rather than discovered in a nightly run.
    """

    correlation_id: str | None
    subject: str | None
    mode: str
    seed: str | None


def sent(*item_ids, kind="event", at=1000.0):
    return {
        item: {"trace_id": item, "status": "sent", "type": kind, "timestamp": at}
        for item in item_ids
    }


def injection(item_id, mode="raise_after_delay"):
    return FakeInjection(correlation_id=item_id, subject="chaos.event", mode=mode, seed="s")


def test_an_injected_fault_is_attributed_not_counted_as_a_leak():
    result = soak.classify(sent("msg_1"), set(), [injection("msg_1")], [], 0)

    assert result["unexplained_count"] == 0, result["unexplained"]
    assert result["attributed_count"] == 1
    assert result["attributed"]["msg_1"]["cause"] == "cyanide"
    assert result["attributed"]["msg_1"]["mode"] == "raise_after_delay"


def test_WITHOUT_the_join_the_same_message_is_a_leak():
    """The red this exists to prevent, at the rate it was measured.

    Same ledger, same verdict function, the injection record simply absent --
    which is the state before cliffracer-cyanide kept one. 111 undelivered
    listener dispatches out of 2000 sends, every one reported as a leak.
    """
    undelivered = [f"msg_{i}" for i in range(111)]
    ledger = sent(*[f"msg_{i}" for i in range(2000)])
    delivered = {item for item in ledger if item not in undelivered}
    records = [injection(item) for item in undelivered]

    without = soak.classify(ledger, delivered, [], [], 0)
    assert without["unexplained_count"] == 111, "the measured leak rate is not reproduced"

    with_record = soak.classify(ledger, delivered, records, [], 0)
    assert with_record["unexplained_count"] == 0
    assert with_record["attributed_count"] == 111
    assert with_record["delivered_count"] == 2000 - 111


def test_a_message_nothing_explains_is_still_a_leak():
    """The control that keeps the join from being a way to pass.

    Attribution must not be "anything undelivered is fine". A send with no
    injection and no fault in flight is the failure the soak exists to catch,
    and it has to survive every join added above.
    """
    result = soak.classify(sent("lost_1", "lost_2"), set(), [injection("other")], [], 0)

    assert result["unexplained_count"] == 2, result
    assert result["attributed_count"] == 0


def test_an_injection_for_a_DIFFERENT_message_does_not_excuse_this_one():
    """The join is per message, not "were any faults injected during the run"."""
    result = soak.classify(sent("lost"), set(), [injection("unrelated")], [], 0)

    assert result["unexplained_count"] == 1
    assert "lost" in result["unexplained"]


def test_a_delivered_message_is_not_attributed_even_if_it_was_faulted():
    """`slow` delays a handler; it still runs. That is a delivery, not a fault to excuse."""
    result = soak.classify(sent("msg_1"), {"msg_1"}, [injection("msg_1", mode="slow")], [], 0)

    assert result["delivered_count"] == 1
    assert result["attributed_count"] == 0
    assert result["unexplained_count"] == 0


def test_an_injection_with_no_correlation_id_joins_to_nothing():
    """The failure mode of forgetting to supply an id, made visible.

    `CorrelationExtension` generates an id when the caller supplies none, and
    a generated id matches nothing the soak knows. If that regresses, this test
    is what says so rather than a nightly run reporting phantom leaks.
    """
    orphan = FakeInjection(
        correlation_id=None, subject="chaos.event", mode="raise_after_delay", seed="s"
    )
    result = soak.classify(sent("msg_1"), set(), [orphan], [], 0)

    assert result["unexplained_count"] == 1


def outage(kind="broker_restart", start=1000.0, end=1002.0):
    return {"time": start, "fault": kind, "until": end}


def test_a_message_sent_into_an_outage_is_attributed():
    """Infrastructure faults have no per-message record, so they are joined by interval."""
    result = soak.classify(sent("msg_1", at=1001.0), set(), [], [outage()], 0, run_ended=1100.0)

    assert result["attributed"]["msg_1"]["cause"] == "broker_restart"


def test_a_message_sent_AFTER_the_outage_ended_is_still_a_leak():
    """The whole point of measuring the interval rather than guessing a window.

    With the fixed +/-6s window this harness started with, a send four seconds
    after the broker came back was excused. Measured on a real 60-second run
    with six restarts, those windows covered 70% of it -- so a genuine leak in
    event or JetStream traffic was near-certain to land inside one. The monkey
    faults every one to five seconds, so over two hours the windows covered
    effectively everything and the soak could not have reported a leak at all.
    """
    result = soak.classify(
        sent("msg_1", at=1006.0), set(), [], [outage(end=1002.0)], 0, run_ended=1100.0
    )

    assert result["unexplained_count"] == 1, (
        "a message sent after the broker was back was excused by the restart"
    )


def test_an_rpc_still_awaiting_its_reply_when_the_broker_dies_is_attributed():
    """The case a real 60-second run reported as a leak, and it was not one.

    An RPC sent 0.9s before a restart is still inside its own reply deadline
    when the broker goes down. The window comes from `RPC_TIMEOUT`, the same
    constant the client waits on, so the verdict cannot allow a window the
    client does not actually wait.
    """
    result = soak.classify(
        sent("msg_1", kind="rpc", at=1000.0 - soak.RPC_TIMEOUT / 2),
        set(),
        [],
        [outage()],
        0,
        run_ended=1100.0,
    )

    assert result["attributed"]["msg_1"]["cause"] == "broker_restart"


def test_a_publish_just_before_an_outage_is_NOT_attributed():
    """A publish has no reply to lose: it went out or it raised.

    The other side of the rule above, so the RPC allowance cannot quietly
    become a grace period for everything.
    """
    result = soak.classify(
        sent("msg_1", kind="event", at=1000.0 - 0.5), set(), [], [outage()], 0, run_ended=1100.0
    )

    assert result["unexplained_count"] == 1


def test_an_outage_that_never_ended_is_open_to_the_end_of_the_run():
    """Not knowing when a fault stopped is a reason to excuse more, not fewer."""
    never = {"time": 1000.0, "fault": "broker_restart", "until": None}
    result = soak.classify(sent("msg_1", at=1050.0), set(), [], [never], 0, run_ended=1100.0)

    assert result["attributed"]["msg_1"]["cause"] == "broker_restart"


def test_each_excuse_class_accounts_for_exactly_its_own_share():
    """Remove one excuse at a time; the leak count must move by that much and no more.

    This is the check that found the defect this file exists around. Replaying
    a real run's undelivered messages with the injection record removed should
    have left every one of them unexplained. It left three, because the
    infrastructure windows had quietly absorbed the other fourteen -- so two
    excuses were covering the same messages and either could be broken without
    the other noticing.

    Asserted as an identity rather than a threshold: an excuse that absorbs
    another's work makes the difference smaller than that class's own count,
    and this reds.
    """
    faults = [outage(start=1000.0, end=1001.0)]
    # by_cyanide sits CLOSE to the outage on purpose. That is the real-run
    # shape: faults land every few seconds, so an injected fault and an outage
    # are usually near each other in time. A fixture that spaces them out
    # cannot catch an excuse reaching past its own interval, which is the
    # defect this test is for.
    ledger = {
        **sent("by_cyanide", kind="event", at=1003.0),
        **sent("by_outage", kind="event", at=1000.5),
        **sent("by_nothing", kind="event", at=3000.0),
    }
    records = [injection("by_cyanide")]

    full = soak.classify(ledger, set(), records, faults, 0, run_ended=4000.0)
    assert full["unexplained_count"] == 1, full["unexplained"]

    cyanide_share = sum(1 for v in full["attributed"].values() if v["cause"] == "cyanide")
    outage_share = sum(1 for v in full["attributed"].values() if v["cause"] != "cyanide")
    assert cyanide_share == 1 and outage_share == 1, full["attributed"]

    without_record = soak.classify(ledger, set(), [], faults, 0, run_ended=4000.0)
    assert without_record["unexplained_count"] - full["unexplained_count"] == cyanide_share, (
        "removing the injection record did not move the leak count by the number of "
        "messages it was excusing, so another excuse is absorbing them"
    )

    without_outages = soak.classify(ledger, set(), records, [], 0, run_ended=4000.0)
    assert without_outages["unexplained_count"] - full["unexplained_count"] == outage_share, (
        "removing the outages did not move the leak count by the number of messages "
        "they were excusing, so another excuse is absorbing them"
    )


@pytest.fixture
def fault_schedule(monkeypatch):
    """Drive the module-level schedule the two closers mutate."""
    schedule: list[dict] = []
    monkeypatch.setattr(soak, "FAULT_SCHEDULE", schedule)
    return schedule


def test_reconnecting_closes_EVERY_open_client_disconnect(fault_schedule):
    """Two disconnects can land before the generator notices either.

    Closing only the newest leaves the older one open, and an open outage runs
    to the end of the run by design -- so one stale entry excuses every message
    after it. Measured on a 60-second run before the fix: the outages absorbed
    20 of the 23 messages cyanide had actually caused.

    They are all outages of one connection. If we are connected now, none of
    them is still running.
    """
    fault_schedule.extend(
        [
            {"time": 1000.0, "fault": "client_disconnect", "until": None},
            {"time": 1002.0, "fault": "client_disconnect", "until": None},
        ]
    )

    soak._close_open_disconnect()

    assert [event for event in fault_schedule if event["until"] is None] == [], (
        "a disconnect was left open, so everything after it would be excused"
    )


async def test_resubscribing_closes_EVERY_open_service_outage(fault_schedule):
    """`_service_reconnected` has the same shape and needs the same property."""
    fault_schedule.extend(
        [
            {"time": 1000.0, "fault": "service_disconnected", "until": None},
            {"time": 1002.0, "fault": "service_disconnected", "until": None},
        ]
    )

    await soak._service_reconnected()

    assert [event for event in fault_schedule if event["until"] is None] == []


def test_a_closer_leaves_other_peoples_outages_alone(fault_schedule):
    """The near miss: closing all of MINE is not closing all of everything.

    A client-disconnect closer that also stamped the service's outages would
    pass the test above while reporting the service back before it was.
    """
    fault_schedule.extend(
        [
            {"time": 1000.0, "fault": "client_disconnect", "until": None},
            {"time": 1001.0, "fault": "service_disconnected", "until": None},
        ]
    )

    soak._close_open_disconnect()

    still_open = [event["fault"] for event in fault_schedule if event["until"] is None]
    assert still_open == ["service_disconnected"], still_open


def test_an_outage_left_open_excuses_every_later_message():
    """What a stale open entry costs, stated as the consequence rather than the state.

    The open-ended default is conservative in the safe direction -- it will not
    invent a leak -- and unsafe in the other, which is exactly why the closers
    have to close all of them.
    """
    late = sent("msg_1", kind="event", at=5000.0)
    stale = {"time": 1000.0, "fault": "client_disconnect", "until": None}
    closed = {"time": 1000.0, "fault": "client_disconnect", "until": 1001.0}

    assert soak.classify(late, set(), [], [stale], 0, run_ended=6000.0)["unexplained_count"] == 0
    assert soak.classify(late, set(), [], [closed], 0, run_ended=6000.0)["unexplained_count"] == 1


def test_a_dropped_record_refuses_to_conclude():
    """A bounded record that evicted something cannot prove a message was NOT faulted.

    Reporting leaks from an incomplete record is how the bound becomes a source
    of false failures. The verdict has to say it cannot answer.
    """
    result = soak.classify(sent("msg_1"), set(), [], [], 4)

    assert result["record_incomplete"] is True
    assert result["injections_dropped"] == 4


def test_an_intact_record_does_not_claim_to_be_incomplete():
    """The other side of it, so `record_incomplete` is not always true."""
    result = soak.classify(sent("msg_1"), {"msg_1"}, [], [], 0)

    assert result["record_incomplete"] is False


# --- the soak's own blind spot, reported every run ---------------------------


def outages(*spans, kind="broker_restart"):
    return [{"time": start, "fault": kind, "until": end} for start, end in spans]


def test_no_outages_excuse_nothing():
    """The floor. Without this, any fraction below one could pass for correct."""
    result = soak.excused_fraction([], 1000.0, 1100.0)

    assert result["excused_fraction_publish"] == 0.0
    assert result["excused_fraction_rpc"] == 0.0
    assert result["outage_count"] == 0
    assert result["run_seconds"] == 100.0


def test_the_excused_fraction_is_the_share_of_wall_clock_an_outage_covers():
    result = soak.excused_fraction(outages((1000.0, 1025.0)), 1000.0, 1100.0)

    assert result["excused_seconds_publish"] == 25.0
    assert result["excused_fraction_publish"] == 0.25


def test_overlapping_outages_are_counted_once():
    """Two faults over one stretch do not excuse it twice.

    Without the union, a burst of faults reads as more coverage than the run
    actually has -- and this number exists to be compared against itself over
    time, so an inflated one is worse than none.
    """
    result = soak.excused_fraction(outages((1000.0, 1030.0), (1010.0, 1020.0)), 1000.0, 1100.0)

    assert result["excused_seconds_publish"] == 30.0, "the overlap was double counted"


def test_an_outage_outside_the_run_is_clipped():
    """A fault before the first send or after the last excuses nothing in between."""
    result = soak.excused_fraction(outages((900.0, 1010.0)), 1000.0, 1100.0)

    assert result["excused_seconds_publish"] == 10.0


def test_an_rpc_is_exposed_for_longer_than_a_publish():
    """An RPC's reply is outstanding for RPC_TIMEOUT, so its exposure starts earlier.

    One number for both transports would understate the RPC case, which is the
    transport most likely to be excused.
    """
    result = soak.excused_fraction(outages((1050.0, 1051.0)), 1000.0, 1100.0)

    assert result["excused_seconds_publish"] == 1.0
    assert result["excused_seconds_rpc"] == 1.0 + soak.RPC_TIMEOUT
    assert result["excused_fraction_rpc"] > result["excused_fraction_publish"]


def test_an_outage_with_no_observed_end_is_excused_to_the_end_of_the_run():
    """Consistent with the verdict, which treats an open outage the same way."""
    open_ended = [{"time": 1050.0, "fault": "client_disconnect", "until": None}]
    result = soak.excused_fraction(open_ended, 1000.0, 1100.0)

    assert result["excused_seconds_publish"] == 50.0


def test_a_cyanide_seed_change_is_not_an_outage():
    """It excuses nothing: cyanide injections are joined per message, not by time."""
    seeds = [{"time": 1010.0, "fault": "cyanide_seed", "seed": "x"}]
    result = soak.excused_fraction(seeds, 1000.0, 1100.0)

    assert result["outage_count"] == 0
    assert result["excused_fraction_publish"] == 0.0


def test_a_run_with_no_sends_reports_zero_rather_than_dividing_by_it():
    """A soak that sent nothing has no wall-clock to be a fraction of."""
    result = soak.excused_fraction(outages((1000.0, 1001.0)), 1000.0, 1000.0)

    assert result["run_seconds"] == 0.0
    assert result["excused_fraction_publish"] == 0.0

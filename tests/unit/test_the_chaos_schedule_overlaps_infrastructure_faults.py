"""The chaos soak runs bursts in which two infrastructure faults are open at once.

A fixed fault interval of 15s keeps the share of a run that nothing can be judged in small, and
costs the one case where two outages overlap: at 15s a broker restart rarely begins while a client
disconnect is open, and the system's behaviour then is exercised by nothing. The monkey now keeps
most of a run quiet and ends each cycle with a burst, in which a restart carries a client
disconnect started inside it, so the overlap is made rather than hoped for. Each fault records the
phase it fell in, and the excused fraction is reported per phase, so a burst that excuses most of
its own window reads as expected and a quiet phase doing the same does not.

The monkey is driven with a fake `docker` and a fake client, as the other soak tests drive the
verdict with fixtures: the question is what the schedule does, not whether NATS delivers.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def _load_soak():
    path = REPO / "load-testing" / "chaos_soak.py"
    spec = importlib.util.spec_from_file_location("chaos_soak_phases", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["chaos_soak_phases"] = module
    spec.loader.exec_module(module)
    return module


soak = _load_soak()


@pytest.fixture(autouse=True)
def _clean_run_state():
    soak.FAULT_SCHEDULE.clear()
    soak.PHASE_ORIGIN.clear()
    soak.CURRENT_PHASE[:] = ["quiet"]
    yield
    soak.FAULT_SCHEDULE.clear()
    soak.PHASE_ORIGIN.clear()


# ---- the phases ----


@pytest.mark.parametrize(
    ("elapsed", "phase"),
    [(0.0, "quiet"), (104.9, "quiet"), (105.0, "burst"), (119.9, "burst"), (120.0, "quiet")],
)
def test_the_last_seconds_of_each_cycle_are_the_burst(elapsed, phase):
    assert soak.phase_at(elapsed, cycle=120.0, burst_seconds=15.0) == phase


def test_the_pattern_repeats_every_cycle():
    assert soak.phase_at(120.0 + 110.0, cycle=120.0, burst_seconds=15.0) == "burst"
    assert soak.phase_at(240.0 + 5.0, cycle=120.0, burst_seconds=15.0) == "quiet"


@pytest.mark.parametrize("burst_seconds", [0.0, -1.0, 120.0, 500.0])
def test_a_burst_that_is_off_or_fills_the_cycle_leaves_the_run_quiet(burst_seconds):
    assert all(
        soak.phase_at(t, cycle=120.0, burst_seconds=burst_seconds) == "quiet"
        for t in (0.0, 60.0, 119.0, 130.0)
    )


def test_a_gap_that_would_cross_into_a_burst_is_cut_at_the_boundary():
    """A quiet 15s gap that began 2s before a burst must not sleep through it."""
    wait, phase = soak.next_fault(
        103.0, cycle=120.0, burst_seconds=15.0, quiet_gap=15.0, burst_gap=1.0
    )

    assert phase == "burst"
    assert wait == pytest.approx(105.0 + 1.0 - 103.0)


def test_a_gap_inside_a_burst_that_would_cross_out_of_it_is_cut_at_the_boundary():
    wait, phase = soak.next_fault(
        118.5, cycle=120.0, burst_seconds=15.0, quiet_gap=15.0, burst_gap=2.0
    )

    assert phase == "quiet"
    assert wait == pytest.approx(120.0 + 15.0 - 118.5)


def test_a_gap_inside_one_phase_is_taken_whole():
    assert soak.next_fault(
        10.0, cycle=120.0, burst_seconds=15.0, quiet_gap=15.0, burst_gap=1.0
    ) == (
        15.0,
        "quiet",
    )
    wait, phase = soak.next_fault(
        106.0, cycle=120.0, burst_seconds=15.0, quiet_gap=15.0, burst_gap=1.0
    )
    assert (round(wait, 6), phase) == (1.0, "burst")


def test_with_no_bursts_the_gap_is_always_the_quiet_gap():
    for elapsed in (0.0, 110.0, 3000.0):
        assert soak.next_fault(
            elapsed, cycle=120.0, burst_seconds=0.0, quiet_gap=15.0, burst_gap=1.0
        ) == (15.0, "quiet")


def test_the_phase_spans_partition_the_run():
    spans = soak.phase_spans(1000.0, 1000.0, 1250.0, cycle=120.0, burst_seconds=15.0)

    quiet = sum(e - s for s, e in spans["quiet"])
    burst = sum(e - s for s, e in spans["burst"])
    assert quiet + burst == pytest.approx(250.0)
    assert burst == pytest.approx(15.0 + 15.0)  # the bursts at 105-120s and 225-240s
    assert spans["burst"][0] == (1105.0, 1120.0)


# ---- the report ----


def fault(kind, at, until, phase):
    return {"time": at, "fault": kind, "until": until, "phase": phase}


def test_two_outages_that_overlap_are_one_compound_outage():
    faults = [
        fault("broker_restart", 100.0, 102.0, "burst"),
        fault("client_disconnect", 101.0, 103.0, "burst"),
    ]

    assert soak.compound_outages(faults, 0.0, 200.0) == [(100.0, 103.0, 2)]


def test_outages_that_do_not_touch_are_not_compound():
    faults = [
        fault("broker_restart", 100.0, 102.0, "quiet"),
        fault("client_disconnect", 115.0, 116.0, "quiet"),
    ]

    assert soak.compound_outages(faults, 0.0, 200.0) == []


def test_an_outage_with_no_observed_end_overlaps_every_later_one():
    faults = [
        fault("client_disconnect", 100.0, None, "burst"),
        fault("broker_restart", 150.0, 152.0, "burst"),
    ]

    assert soak.compound_outages(faults, 0.0, 200.0) == [(100.0, 200.0, 2)]


def test_the_services_own_disconnect_nesting_in_a_restart_is_not_a_compound_outage():
    """A restart takes the service's connection with it, and its gap outlasts the restart's, so the
    two nest on every restart. That is one fault and its consequence, not two faults."""
    faults = [
        fault("broker_restart", 100.0, 100.4, "quiet"),
        fault("service_disconnected", 100.1, 102.0, "quiet"),
    ]

    assert soak.compound_outages(faults, 0.0, 200.0) == []


def test_a_service_disconnect_does_not_join_two_injected_outages_into_a_third_group():
    faults = [
        fault("broker_restart", 100.0, 101.0, "burst"),
        fault("service_disconnected", 100.5, 105.0, "burst"),
        fault("client_disconnect", 104.0, 106.0, "burst"),
    ]

    assert soak.compound_outages(faults, 0.0, 200.0) == []


def test_a_seed_change_is_not_an_outage_so_it_never_makes_one_compound():
    faults = [
        fault("broker_restart", 100.0, 102.0, "burst"),
        {"time": 101.0, "fault": "cyanide_seed", "seed": "s", "phase": "burst"},
    ]

    assert soak.compound_outages(faults, 0.0, 200.0) == []


def test_the_excused_fraction_is_reported_against_each_phases_own_seconds():
    phases = {"quiet": [(0.0, 100.0)], "burst": [(100.0, 120.0)]}
    faults = [
        fault("broker_restart", 10.0, 11.0, "quiet"),  # 1s of a 100s phase
        fault("broker_restart", 102.0, 108.0, "burst"),  # 6s of a 20s phase
        fault("client_disconnect", 105.0, 110.0, "burst"),  # overlaps the one above
    ]

    report = soak.excused_fraction(faults, 0.0, 120.0, phases)

    assert report["phases"]["quiet"]["excused_fraction_publish"] == pytest.approx(0.01)
    assert report["phases"]["quiet"]["compound_outages"] == 0
    assert report["phases"]["burst"]["excused_fraction_publish"] == pytest.approx(8.0 / 20.0)
    assert report["phases"]["burst"]["compound_outages"] == 1
    assert report["phases"]["burst"]["outage_count"] == 2
    assert report["compound_outages"] == 1


def test_an_outage_that_straddles_a_boundary_is_split_between_the_phases():
    phases = {"quiet": [(0.0, 100.0)], "burst": [(100.0, 120.0)]}

    report = soak.excused_fraction(
        [fault("broker_restart", 98.0, 103.0, "quiet")], 0.0, 120.0, phases
    )

    assert report["phases"]["quiet"]["excused_fraction_publish"] == pytest.approx(2.0 / 100.0)
    assert report["phases"]["burst"]["excused_fraction_publish"] == pytest.approx(3.0 / 20.0)


def test_CONTROL_without_phases_the_report_has_its_old_shape_plus_the_compound_count():
    report = soak.excused_fraction([fault("broker_restart", 10.0, 12.0, "quiet")], 0.0, 100.0)

    assert "phases" not in report
    assert report["excused_fraction_publish"] == pytest.approx(0.02)
    assert report["compound_outages"] == 0


# ---- the monkey ----


class FakeClient:
    def __init__(self) -> None:
        self.is_closed = False
        self.closed_at: float | None = None

    async def close(self) -> None:
        self.is_closed = True


class FakeProcess:
    def __init__(self, seconds: float) -> None:
        self._seconds = seconds

    async def communicate(self):
        await asyncio.sleep(self._seconds)
        return b"", b""


@pytest.fixture
def fake_docker(monkeypatch):
    async def create(*argv, **kwargs):
        return FakeProcess(0.4)

    monkeypatch.setattr(soak.asyncio, "create_subprocess_exec", create)


async def _run_monkey(seconds: float, **options):
    stop = asyncio.Event()
    service = SimpleNamespace(cyanide=SimpleNamespace(config=SimpleNamespace(seed="s")))
    client = FakeClient()
    task = asyncio.create_task(soak.chaos_monkey(service, client, stop, "nats-chaos", **options))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    return client


async def test_a_burst_starts_a_disconnect_inside_a_broker_restart(fake_docker, monkeypatch):
    monkeypatch.setattr(soak.random, "choice", lambda options: "compound_outage")

    await _run_monkey(
        1.6,
        interval_min=60.0,
        interval_max=60.0,
        phase_cycle=1.0,
        burst_seconds=0.9,
        burst_interval=0.1,
    )

    restarts = [e for e in soak.FAULT_SCHEDULE if e["fault"] == "broker_restart"]
    disconnects = [e for e in soak.FAULT_SCHEDULE if e["fault"] == "client_disconnect"]
    assert restarts and disconnects
    first_restart = restarts[0]
    inside = [
        d for d in disconnects if first_restart["time"] <= d["time"] <= first_restart["until"]
    ]
    assert inside, "no client disconnect began inside a broker restart"
    assert {e["phase"] for e in restarts + disconnects} == {"burst"}
    assert soak.compound_outages(soak.FAULT_SCHEDULE, 0.0, first_restart["until"] + 1) != []


async def test_the_quiet_phase_injects_one_fault_at_a_time_and_none_during_the_wait(
    fake_docker, monkeypatch
):
    monkeypatch.setattr(soak.random, "choice", lambda options: "broker_restart")

    await _run_monkey(
        1.2,
        interval_min=0.3,
        interval_max=0.3,
        phase_cycle=1000.0,
        burst_seconds=0.0,
        burst_interval=0.1,
    )

    restarts = [e for e in soak.FAULT_SCHEDULE if e["fault"] == "broker_restart"]
    assert len(restarts) >= 2
    assert {e["phase"] for e in soak.FAULT_SCHEDULE} == {"quiet"}
    assert not any(e["fault"] == "client_disconnect" for e in soak.FAULT_SCHEDULE)
    assert soak.compound_outages(soak.FAULT_SCHEDULE, 0.0, 10**10) == []


async def test_the_quiet_phase_of_a_cycle_has_no_burst_faults_in_it(fake_docker, monkeypatch):
    """Quiet gap 60s, burst from 0.6s: nothing is injected until the burst begins."""
    monkeypatch.setattr(soak.random, "choice", lambda options: "compound_outage")

    await _run_monkey(
        0.55,
        interval_min=60.0,
        interval_max=60.0,
        phase_cycle=1.0,
        burst_seconds=0.4,
        burst_interval=0.1,
    )

    assert soak.FAULT_SCHEDULE == []


async def test_a_service_outage_the_monkey_did_not_start_carries_the_current_phase(fake_docker):
    soak.CURRENT_PHASE[:] = ["burst"]

    await soak._service_disconnected()

    assert soak.FAULT_SCHEDULE[-1]["phase"] == "burst"


async def test_a_fault_is_not_injected_after_the_run_was_told_to_stop(fake_docker, monkeypatch):
    """The monkey sleeps toward its next fault; a stop wakes it rather than letting it fire."""
    monkeypatch.setattr(soak.random, "choice", lambda options: "broker_restart")

    await _run_monkey(
        0.2,
        interval_min=0.5,
        interval_max=0.5,
        phase_cycle=1000.0,
        burst_seconds=0.0,
        burst_interval=0.1,
    )
    await asyncio.sleep(0.6)

    assert soak.FAULT_SCHEDULE == []


async def test_a_burst_draws_from_compound_outages_and_a_quiet_phase_does_not(
    fake_docker, monkeypatch
):
    """Every other test here fixes the draw; this reads what each phase is offered."""
    offered: list[tuple[str, ...]] = []

    def choose(options):
        offered.append(tuple(options))
        return "cyanide_seed"

    monkeypatch.setattr(soak.random, "choice", choose)

    await _run_monkey(
        1.3,
        interval_min=0.15,
        interval_max=0.15,
        phase_cycle=1.0,
        burst_seconds=0.5,
        burst_interval=0.05,
    )

    quiet = {o for o, e in zip(offered, soak.FAULT_SCHEDULE, strict=False) if e["phase"] == "quiet"}
    burst = {o for o, e in zip(offered, soak.FAULT_SCHEDULE, strict=False) if e["phase"] == "burst"}
    assert quiet and burst
    assert all("compound_outage" not in options for options in quiet)
    assert all("compound_outage" in options for options in burst)
    assert all("broker_restart" in options and "client_disconnect" in options for options in quiet)


async def test_the_monkey_publishes_the_phase_it_is_in_for_outages_it_does_not_start(
    fake_docker, monkeypatch
):
    """The service's own disconnect is recorded by a callback; the phase it reads is the monkey's."""
    monkeypatch.setattr(soak.random, "choice", lambda options: "cyanide_seed")

    await _run_monkey(
        0.9,
        interval_min=60.0,
        interval_max=60.0,
        phase_cycle=1.0,
        burst_seconds=0.6,
        burst_interval=0.05,
    )
    await soak._service_disconnected()

    assert soak.CURRENT_PHASE[0] == "burst"
    assert soak.FAULT_SCHEDULE[-1]["fault"] == "service_disconnected"
    assert soak.FAULT_SCHEDULE[-1]["phase"] == "burst"

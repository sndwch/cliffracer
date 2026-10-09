import argparse
import asyncio
import json
import logging
import random
import sys
import time
import uuid

import nats
from cliffracer_cyanide import CyanideConfig, CyanideExtension
from cliffracer_otel import OtelExtension
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.client import ServiceClient
from cliffracer.core.jetstream import StreamSpec

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# The client's reply deadline. Read by the verdict too: an RPC sent before an
# outage is still waiting for its reply during it, so how long it waits is
# exactly how far back an outage can reach. Two copies of this number would
# be two different answers to that question.
RPC_TIMEOUT = 2.0

# Seconds between injected infrastructure faults. Measured, not chosen: at the
# 1-5s this harness started with, outages excused 24% of a run's wall-clock for
# a publish and 52% for an RPC -- a real leak in RPC traffic had roughly even
# odds of being reported. At a fixed 15s that falls to 4% and 9%.
#
# It costs nothing that matters. Cyanide fires per message on a weight, so its
# attribution is flat across every interval measured (45, 42, 40, 45 over four
# arms); the interval buys outages, and outages are the blind spot. Over the
# two-hour default this still yields on the order of 480 faults.
DEFAULT_FAULT_INTERVAL = 15.0

# A run is mostly quiet and ends each cycle with a burst. In a burst the monkey faults every
# second and starts a client disconnect while a broker restart is still in flight, so two
# infrastructure faults are open at once. At 15s they rarely are, and what the system does when
# they are is then exercised by nothing: the service's resubscription gap outlasts the restart
# that caused it (0.33s of container downtime against a 2.01s service gap), so a disconnect that
# lands inside that window is the next case along.
#
# What a burst can find is damage that outlasts the compound outage. Everything inside it is
# excused, as any outage is, and says nothing; a subscription left dead afterwards loses messages
# that are sent after both faults have ended, which no excuse covers, and the verdict reports
# them. The quiet phases keep the reporting odds the fixed interval bought, and the ledger says
# how much of each phase an outage excused, so a burst excusing most of its own window reads as
# expected and a quiet phase doing the same reads as a regression.
DEFAULT_PHASE_CYCLE = 120.0
DEFAULT_BURST_SECONDS = 15.0
DEFAULT_BURST_INTERVAL = 1.0
# Set when the monkey starts: the zero the phases are counted from.
PHASE_ORIGIN: list[float] = []
# The phase an outage that the monkey did not start (the service losing the broker) falls in.
CURRENT_PHASE: list[str] = ["quiet"]


def phase_at(elapsed: float, cycle: float, burst_seconds: float) -> str:
    """The phase `elapsed` seconds into a run falls in: a burst ends every `cycle` seconds."""
    if burst_seconds <= 0 or burst_seconds >= cycle:
        return "quiet"
    return "burst" if (elapsed % cycle) >= cycle - burst_seconds else "quiet"


def _next_boundary(elapsed: float, cycle: float, burst_seconds: float) -> float:
    """The next elapsed time at which the phase changes, or infinity when there are no bursts."""
    if burst_seconds <= 0 or burst_seconds >= cycle:
        return float("inf")
    into = elapsed % cycle
    start_of_cycle = elapsed - into
    burst_starts = cycle - burst_seconds
    return start_of_cycle + (burst_starts if into < burst_starts else cycle)


def next_fault(
    elapsed: float, *, cycle: float, burst_seconds: float, quiet_gap: float, burst_gap: float
) -> tuple[float, str]:
    """Seconds until the next fault, and the phase it lands in, `elapsed` seconds into a run.

    A gap that would run past a phase boundary is cut at the boundary and drawn again in the new
    phase. Otherwise a 15-second quiet gap that began just before a burst would sleep through
    most of it.
    """
    t = elapsed
    while True:
        phase = phase_at(t, cycle, burst_seconds)
        gap = burst_gap if phase == "burst" else quiet_gap
        boundary = _next_boundary(t, cycle, burst_seconds)
        if t + gap <= boundary:
            return t + gap - elapsed, phase
        t = boundary


def phase_spans(
    origin: float, run_started: float, run_ended: float, cycle: float, burst_seconds: float
) -> dict[str, list[tuple[float, float]]]:
    """The wall-clock spans of the run that fall in each phase."""
    spans: dict[str, list[tuple[float, float]]] = {"quiet": [], "burst": []}
    t = run_started
    while t < run_ended:
        phase = phase_at(t - origin, cycle, burst_seconds)
        end = min(origin + _next_boundary(t - origin, cycle, burst_seconds), run_ended)
        if end <= t:
            break
        spans[phase].append((t, end))
        t = end
    return spans


LEDGER_SENT: dict[str, dict] = {}
# Delivered means THE HANDLER BODY RAN. Recorded there and nowhere else.
#
# An earlier version of this harness recorded delivery from an extension's
# worker_setup, which runs before the handler. That does make the cyanide
# problem go away -- a refused message is marked delivered -- but it also makes
# the soak unable to see a message that reached the service and was lost inside
# the handler, which is most of what it is for. Attribution belongs at the
# verdict, not in the marker.
LEDGER_DELIVERED: set[str] = set()
# Infrastructure faults only: broker restarts and client disconnects. Cyanide
# injections are not here; they come from the extension's own record.
FAULT_SCHEDULE: list[dict] = []
# Cyanide's record, drained into this list while the soak runs.
INJECTIONS: list = []


class ChaosMessage(BaseModel):
    item_id: str


class ChaosResponse(BaseModel):
    status: str
    id: str


class ChaosService(CliffracerService):
    cyanide = CyanideExtension(
        config=CyanideConfig(
            enabled=True,
            mode="random",
            slow_weight=0.05,
            drop_reply_weight=0.05,
            raise_after_delay_weight=0.05,
            sleep_past_timeout_weight=0.05,
        )
    )
    otel = OtelExtension()

    @rpc
    async def process_rpc(self, item_id: str) -> ChaosResponse:
        LEDGER_DELIVERED.add(item_id)
        return ChaosResponse(status="ok", id=item_id)

    @listener("chaos.event", fanout=True)
    async def process_event(self, msg: ChaosMessage) -> None:
        LEDGER_DELIVERED.add(msg.item_id)

    @listener("chaos.js", durable="chaos-consumer")
    async def process_js(self, msg: ChaosMessage) -> None:
        LEDGER_DELIVERED.add(msg.item_id)


async def _client_disconnect(nc_generator: nats.NATS, phase: str) -> None:
    logger.info("Chaos Monkey: Simulating client disconnect")
    # `until` is filled in by the traffic generator when it reconnects.
    # Left open until then, which is the conservative reading: a fault
    # whose end was never observed excuses everything after it rather
    # than pretending to know when it stopped.
    FAULT_SCHEDULE.append(
        {"time": time.time(), "fault": "client_disconnect", "until": None, "phase": phase}
    )
    if not nc_generator.is_closed:
        await nc_generator.close()


async def _broker_restart(container_name: str, phase: str) -> None:
    logger.info("Chaos Monkey: Restarting broker")
    event = {"time": time.time(), "fault": "broker_restart", "until": None, "phase": phase}
    FAULT_SCHEDULE.append(event)
    try:
        proc = await asyncio.create_subprocess_exec("docker", "restart", container_name)
        await proc.communicate()
    except Exception as e:
        logger.error(f"Failed to restart broker: {e}")
    finally:
        # The measured end of the outage, not a guess about it.
        event["until"] = time.time()


async def _compound_outage(nc_generator: nats.NATS, container_name: str, phase: str) -> None:
    """A broker restart with a client disconnect started while it is still in flight.

    The two outages overlap by construction, not by the luck of an interval: the disconnect
    begins inside the restart's recorded span. Both are recorded the usual way, each with its own
    measured end, so the join and the excused fraction treat them as the overlapping outages they
    are.
    """
    restart = asyncio.create_task(_broker_restart(container_name, phase))
    await asyncio.sleep(random.uniform(0.05, 0.3))
    await _client_disconnect(nc_generator, phase)
    await restart


async def chaos_monkey(
    service: ChaosService,
    nc_generator: nats.NATS,
    stop_event: asyncio.Event,
    container_name: str,
    interval_min: float = DEFAULT_FAULT_INTERVAL,
    interval_max: float = DEFAULT_FAULT_INTERVAL,
    phase_cycle: float = DEFAULT_PHASE_CYCLE,
    burst_seconds: float = DEFAULT_BURST_SECONDS,
    burst_interval: float = DEFAULT_BURST_INTERVAL,
):
    """Inject a fault every `interval_min`-`interval_max` seconds, and burst at the end of a cycle.

    The interval is the lever on how much of a run the infrastructure excuses:
    each restart or disconnect buys an interval nothing can be judged in, so
    faulting often makes the soak busy and blind at the same time. Most of a
    run is therefore quiet. The last `burst_seconds` of every `phase_cycle`
    seconds are a burst: a fault every `burst_interval` seconds, drawn from
    compound outages (a restart with a disconnect inside it) and seed changes, so the
    case the quiet phases cannot reach is exercised on purpose. `burst_seconds=0` is no bursts.
    """
    logger.info(
        "Chaos monkey started, faulting every %.1f-%.1fs; a %.1fs burst every %.1fs",
        interval_min,
        interval_max,
        burst_seconds,
        phase_cycle,
    )
    started = time.time()
    PHASE_ORIGIN[:] = [started]
    while not stop_event.is_set():
        wait, phase = next_fault(
            time.time() - started,
            cycle=phase_cycle,
            burst_seconds=burst_seconds,
            quiet_gap=random.uniform(interval_min, interval_max),
            burst_gap=burst_interval,
        )
        # Woken by the stop, not slept through: a fault must not land after the run was told to end.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait)
            break
        except TimeoutError:
            pass
        CURRENT_PHASE[:] = [phase]

        if phase == "burst":
            fault = random.choice(["cyanide_seed", "compound_outage", "compound_outage"])
        else:
            fault = random.choice(
                ["cyanide_seed", "client_disconnect", "broker_restart", "cyanide_seed"]
            )

        if fault == "cyanide_seed":
            new_seed = str(uuid.uuid4())
            service.cyanide.config.seed = new_seed
            logger.info(f"Chaos Monkey: Changed Cyanide seed to {new_seed}")
            FAULT_SCHEDULE.append(
                {"time": time.time(), "fault": "cyanide_seed", "seed": new_seed, "phase": phase}
            )

        elif fault == "client_disconnect":
            await _client_disconnect(nc_generator, phase)

        elif fault == "broker_restart":
            await _broker_restart(container_name, phase)

        elif fault == "compound_outage":
            await _compound_outage(nc_generator, container_name, phase)


async def _service_disconnected() -> None:
    """The SERVICE lost the broker. Recorded as an outage in its own right.

    Not the same interval as the client's. A core NATS publish is dropped by
    the server when no subscriber is attached, so an event published while the
    service is away is lost with nothing to redeliver it -- and that gap starts
    when the service disconnects and ends when it is back and subscribed, which
    is strictly later than `docker restart` returning.

    Measured: without this, a 60-second run reported one undelivered event per
    run that no fault explained. Over a two-hour soak that is a nightly failure
    with no bug behind it, which is the thing this harness exists to stop doing.
    """
    FAULT_SCHEDULE.append(
        {
            "time": time.time(),
            "fault": "service_disconnected",
            "until": None,
            "phase": CURRENT_PHASE[0],
        }
    )


async def _service_reconnected() -> None:
    """Close every open service outage; we are subscribed again."""
    now = time.time()
    for event in FAULT_SCHEDULE:
        if event["fault"] == "service_disconnected" and event.get("until") is None:
            event["until"] = now


def _close_open_disconnect() -> None:
    """Mark EVERY open client disconnect as over, now that we are back.

    Every one, not the most recent: two disconnects can land before the
    generator notices either, and closing only the newest leaves the older one
    open forever. An open outage runs to the end of the run by design, so that
    one stale entry then excuses every undelivered message after it.

    Measured, because this is not hypothetical -- it is how the first version
    of this function behaved. On a 60-second run one disconnect was left open
    and the replay check showed the outages absorbing 20 of the 23 messages
    cyanide had actually caused. They are all outages of one connection: if we
    are connected now, none of them is still running.

    The monkey knows when it cut the connection; only the generator knows when
    it got one back, so the two halves of the interval are recorded by the two
    sides that can observe them.
    """
    now = time.time()
    for event in FAULT_SCHEDULE:
        if event["fault"] == "client_disconnect" and event.get("until") is None:
            event["until"] = now


async def traffic_generator(nc: nats.NATS, nats_url: str, stop_event: asyncio.Event):
    client = ServiceClient(nc=nc, service="chaos_svc", verify=False, timeout=RPC_TIMEOUT)
    js = nc.jetstream()

    seq = 0
    while not stop_event.is_set():
        if nc.is_closed:
            try:
                await nc.connect(nats_url)
                client = ServiceClient(
                    nc=nc, service="chaos_svc", verify=False, timeout=RPC_TIMEOUT
                )
                js = nc.jetstream()
                _close_open_disconnect()
            except Exception:
                await asyncio.sleep(0.5)
                continue

        seq += 1
        item_id = f"msg_{seq}"

        t_type = random.choice(["rpc", "event", "js"])
        LEDGER_SENT[item_id] = {
            "trace_id": item_id,
            "status": "sent",
            "type": t_type,
            "timestamp": time.time(),
        }

        try:
            if t_type == "rpc":
                # The correlation id IS the join key, and the service will
                # generate one if the caller does not supply it -- an id only
                # the service knows, which joins to nothing here. Set per call
                # rather than per client: this loop is the only writer and it
                # awaits each send, so no two items are in flight at once.
                client.headers["X-Correlation-ID"] = item_id
                client.headers["correlation_id"] = item_id
                try:
                    res = await client._call("process_rpc", {"item_id": item_id}, ChaosResponse)
                    if res.status == "ok":
                        LEDGER_SENT.pop(item_id, None)
                except Exception:
                    pass
            elif t_type == "event":
                await nc.publish(
                    "chaos.event",
                    json.dumps({"item_id": item_id, "correlation_id": item_id}).encode(),
                )
            elif t_type == "js":
                await js.publish(
                    "chaos.js",
                    json.dumps({"item_id": item_id, "correlation_id": item_id}).encode(),
                )
        except Exception as e:
            LEDGER_SENT[item_id]["status"] = f"failed_known: {type(e).__name__}"

        await asyncio.sleep(0.01)


def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The union of `spans`: overlapping spans count once."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _length(spans: list[tuple[float, float]], within: list[tuple[float, float]] | None = None):
    """Seconds in `spans`, or in the part of them that falls inside `within`."""
    if within is None:
        return sum(end - start for start, end in spans)
    return sum(
        max(0.0, min(end, w_end) - max(start, w_start))
        for start, end in spans
        for w_start, w_end in within
    )


def compound_outages(
    faults: list[dict], run_started: float, run_ended: float
) -> list[tuple[float, float, int]]:
    """Each stretch of the run in which two or more INJECTED outages were open at once.

    Returned as (start, end, how many outages), by the outages' measured spans. An outage with no
    observed end is open to the end of the run, as everywhere else in this verdict.

    The service's own disconnect is not counted: it is what a broker restart does to the service,
    so it nests inside the restart that caused it on every restart (its gap outlasts the restart's),
    and counting it would call every restart compound. What this reports is two faults the monkey
    injected overlapping, which is the case a fixed interval never reaches.
    """
    spans = []
    for event in faults:
        if event["fault"] in ("cyanide_seed", "service_disconnected"):
            continue
        until = event.get("until")
        start = max(event["time"], run_started)
        end = min(run_ended if until is None else until, run_ended)
        if end > start:
            spans.append((start, end))
    groups: list[list[float]] = []
    for start, end in sorted(spans):
        if groups and start <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], end)
            groups[-1][2] += 1
        else:
            groups.append([start, end, 1])
    return [(start, end, int(n)) for start, end, n in groups if n >= 2]


def excused_fraction(
    faults: list[dict],
    run_started: float,
    run_ended: float,
    phases: dict[str, list[tuple[float, float]]] | None = None,
) -> dict:
    """How much of the run the infrastructure excuses cover.

    A leak this soak can find is a leak that did not land inside an outage.
    That makes the excused fraction the soak's own blind spot, and reporting it
    every run is what turns a drift back toward a rubber stamp into something
    visible in the ledger rather than something found by replaying a run by
    hand -- which is how it was found the first time, when guessed windows of
    +/-6s and +/-3s covered 70% and 51% of a 60-second run.

    Reported per transport because the reach differs. A publish is excused only
    if it was sent inside an outage; an RPC is excused if its reply was still
    outstanding when one began, so its exposure starts `RPC_TIMEOUT` earlier.
    One number for both would understate the RPC case.

    With `phases` (the wall-clock spans of each phase), the same numbers are also given per
    phase, against that phase's own seconds, and the stretches where two outages overlapped are
    counted (`compound_outages`). A burst excusing most of its own window is expected; a quiet
    phase doing so is not, and one number for the whole run would hide both.

    No threshold is applied. What counts as too much excusing is a judgement
    about how hard this soak should be trying, and that belongs to whoever sets
    the fault interval, not to the function that measures it.
    """
    span = max(run_ended - run_started, 0.0)
    if span <= 0:
        return {
            "run_seconds": 0.0,
            "outage_count": 0,
            "compound_outages": 0,
            "excused_seconds_publish": 0.0,
            "excused_fraction_publish": 0.0,
            "excused_seconds_rpc": 0.0,
            "excused_fraction_rpc": 0.0,
        }

    outages = [event for event in faults if event["fault"] != "cyanide_seed"]

    def covered(lookback: float) -> list[tuple[float, float]]:
        """The outage intervals, clipped to the run, merged. Overlaps count once."""
        spans = []
        for event in outages:
            until = event.get("until")
            if until is None:
                until = run_ended
            start = max(event["time"] - lookback, run_started)
            end = min(until, run_ended)
            if end > start:
                spans.append((start, end))
        return _merge(spans)

    publish_spans = covered(0.0)
    rpc_spans = covered(RPC_TIMEOUT)
    publish = _length(publish_spans)
    rpc = _length(rpc_spans)
    compound = compound_outages(faults, run_started, run_ended)
    result = {
        "run_seconds": round(span, 3),
        "outage_count": len(outages),
        "compound_outages": len(compound),
        "excused_seconds_publish": round(publish, 3),
        "excused_fraction_publish": round(publish / span, 4),
        "excused_seconds_rpc": round(rpc, 3),
        "excused_fraction_rpc": round(rpc / span, 4),
    }
    if phases is not None:
        result["phases"] = {}
        for name, windows in phases.items():
            seconds = _length(windows)
            if seconds <= 0:
                continue
            result["phases"][name] = {
                "seconds": round(seconds, 3),
                "outage_count": sum(
                    1 for e in outages if any(w[0] <= e["time"] < w[1] for w in windows)
                ),
                "compound_outages": sum(
                    1 for c in compound if any(w[0] <= c[0] < w[1] for w in windows)
                ),
                "excused_fraction_publish": round(_length(publish_spans, windows) / seconds, 4),
                "excused_fraction_rpc": round(_length(rpc_spans, windows) / seconds, 4),
            }
    return result


def classify(
    sent: dict[str, dict],
    delivered: set[str],
    injections: list,
    faults: list[dict],
    injections_dropped: int,
    run_ended: float | None = None,
) -> dict:
    """Split every send into delivered, attributed, or unexplained.

    A send that never reached its handler is only a leak if nothing explains
    it. Two things can:

    * a cyanide injection, joined EXACTLY by correlation id from the
      extension's own record. Cyanide refuses a message before the handler
      runs, so a refused listener dispatch looks precisely like a lost one;
      without this join the soak reports every one as a leak. Measured at the
      weights this harness uses: 111 of 2000 listener dispatches, 5.55%.
    * a broker restart or a client disconnect, joined by time, because neither
      leaves a per-message record -- they take out whatever was in flight.

    `injections_dropped` is not decoration. The record is bounded, and if it
    evicted anything then "no injection recorded for this message" no longer
    means "not injected". The verdict says so instead of counting leaks it
    cannot stand behind.
    """
    if run_ended is None:
        run_ended = max((record["timestamp"] for record in sent.values()), default=0.0)

    injected = {
        injection.correlation_id: injection
        for injection in injections
        if injection.correlation_id is not None
    }

    delivered_count = 0
    attributed: dict[str, dict] = {}
    unexplained: dict[str, dict] = {}

    for item_id, record in sent.items():
        if item_id in delivered:
            delivered_count += 1
            continue
        if record.get("status") != "sent":
            continue

        injection = injected.get(item_id)
        if injection is not None:
            attributed[item_id] = {**record, "cause": "cyanide", "mode": injection.mode}
            continue

        cause = _infrastructure_fault(record, faults, run_ended)
        if cause is not None:
            attributed[item_id] = {**record, "cause": cause}
            continue

        unexplained[item_id] = record

    return {
        "delivered_count": delivered_count,
        "attributed_count": len(attributed),
        "attributed": attributed,
        "unexplained_count": len(unexplained),
        "unexplained": unexplained,
        "injections_dropped": injections_dropped,
        "record_incomplete": injections_dropped > 0,
    }


def _delivery_window(record: dict) -> tuple[float, float]:
    """When this message could still have been lost.

    A publish either goes out or raises, so its window is the instant it was
    sent. An RPC is different: it is waiting for a reply for up to
    `RPC_TIMEOUT` afterwards, and an outage that starts inside that wait takes
    the reply with it. The window is derived from the deadline the client
    actually uses rather than guessed, which is why they share a constant.
    """
    sent_at = record["timestamp"]
    if record["type"] == "rpc":
        return sent_at, sent_at + RPC_TIMEOUT
    return sent_at, sent_at


def _infrastructure_fault(record: dict, faults: list[dict], run_ended: float) -> str | None:
    """The outage that overlapped this message's delivery window, if any.

    Joined against the MEASURED interval -- from when the fault was applied to
    when the connection was observed back -- and not a fixed window either side
    of the trigger.

    That distinction is the difference between a leak detector and a rubber
    stamp. This harness started with +/-6s around a broker restart and +/-3s
    around a disconnect. Measured on a 60-second run with nine outages, those
    windows covered 70% and 51% of it, while the real downtime was 2.86s --
    4.8%. A genuine leak was near-certain to fall inside a window belonging to
    a fault that had nothing to do with it, and the monkey faults every one to
    five seconds, so over two hours the windows covered everything.

    Both faults reach every transport. An earlier version excused only events
    and JetStream during a broker restart; a 60-second run then reported an
    undelivered RPC sent 0.9s before a restart as a leak, which it was not --
    an RPC waiting for a reply loses it when the broker goes down like anything
    else.

    An outage with no observed end is open to the end of the run. That is the
    conservative reading and it is deliberate: not knowing when a fault stopped
    is a reason to excuse more, not fewer.
    """
    starts, ends = _delivery_window(record)
    for event in faults:
        if event["fault"] == "cyanide_seed":
            continue
        until = event.get("until")
        if until is None:
            until = run_ended
        if starts <= until and ends >= event["time"]:
            return event["fault"]
    return None


async def drain_injections(service, stop_event: asyncio.Event, interval: float = 1.0) -> None:
    """Move cyanide's record into INJECTIONS while the soak runs.

    The record is bounded and a soak runs for hours, so leaving it to fill is
    how the bound starts dropping and the verdict stops being able to attribute
    anything. Draining is also why the bound can stay small.
    """
    while not stop_event.is_set():
        await asyncio.sleep(interval)
        INJECTIONS.extend(service.cyanide.drain_injections())


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="nats://localhost:4223", help="NATS broker URL")
    parser.add_argument("--duration", type=int, default=30, help="Duration in seconds")
    parser.add_argument("--no-faults", action="store_true", help="Disable chaos monkey")
    parser.add_argument(
        "--container-name", default="nats-chaos", help="Name of the broker container to restart"
    )
    parser.add_argument(
        "--fault-interval-min",
        type=float,
        default=DEFAULT_FAULT_INTERVAL,
        help="Shortest gap between injected faults, in seconds",
    )
    parser.add_argument(
        "--fault-interval-max",
        type=float,
        default=DEFAULT_FAULT_INTERVAL,
        help="Longest gap between injected faults, in seconds",
    )
    parser.add_argument(
        "--phase-cycle",
        type=float,
        default=DEFAULT_PHASE_CYCLE,
        help="Seconds in one cycle: quiet, then a burst at the end of it",
    )
    parser.add_argument(
        "--burst-seconds",
        type=float,
        default=DEFAULT_BURST_SECONDS,
        help="Seconds of each cycle spent in a burst of compound faults; 0 for none",
    )
    parser.add_argument(
        "--burst-interval",
        type=float,
        default=DEFAULT_BURST_INTERVAL,
        help="Seconds between faults during a burst",
    )
    args = parser.parse_args()

    config = ServiceConfig(
        name="chaos_svc",
        nats_url=args.url,
        on_disconnect=_service_disconnected,
        on_connect=_service_reconnected,
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="CHAOS", subjects=["chaos.js"]),
            StreamSpec(name="DLQ", subjects=["dlq.chaos_svc"]),
        ],
    )

    service = ChaosService(config)

    if args.no_faults:
        service.cyanide.config.enabled = False

    await service.start()

    nc_gen = await nats.connect(args.url)
    stop_event = asyncio.Event()

    tasks = [asyncio.create_task(traffic_generator(nc_gen, args.url, stop_event))]

    if not args.no_faults:
        tasks.append(
            asyncio.create_task(
                chaos_monkey(
                    service,
                    nc_gen,
                    stop_event,
                    args.container_name,
                    args.fault_interval_min,
                    args.fault_interval_max,
                    args.phase_cycle,
                    args.burst_seconds,
                    args.burst_interval,
                )
            )
        )
    tasks.append(asyncio.create_task(drain_injections(service, stop_event)))

    await asyncio.sleep(args.duration)
    stop_event.set()

    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info("Waiting for trailing processing to complete...")
    for _ in range(15):
        await asyncio.sleep(1.0)
        unmatched_js = sum(
            1
            for k, v in LEDGER_SENT.items()
            if k not in LEDGER_DELIVERED and v["status"] == "sent" and v["type"] == "js"
        )
        if unmatched_js == 0:
            break

    if not nc_gen.is_closed:
        await nc_gen.close()
    await service.stop()

    # One last drain: faults injected after the previous tick are still in the
    # extension, and those are exactly the ones explaining the final messages.
    INJECTIONS.extend(service.cyanide.drain_injections())

    result = classify(
        LEDGER_SENT,
        LEDGER_DELIVERED,
        INJECTIONS,
        FAULT_SCHEDULE,
        service.cyanide.injections_dropped,
    )
    result["fault_schedule"] = FAULT_SCHEDULE
    sends = [record["timestamp"] for record in LEDGER_SENT.values()]
    run_started, run_ended = min(sends, default=0.0), max(sends, default=0.0)
    coverage = excused_fraction(
        FAULT_SCHEDULE,
        run_started,
        run_ended,
        phase_spans(PHASE_ORIGIN[0], run_started, run_ended, args.phase_cycle, args.burst_seconds)
        if PHASE_ORIGIN
        else None,
    )
    result["coverage"] = coverage

    with open("ledger.json", "w") as f:
        json.dump(result, f, indent=2, default=str)

    logger.info(
        "Soak complete. delivered=%d attributed=%d unexplained=%d",
        result["delivered_count"],
        result["attributed_count"],
        result["unexplained_count"],
    )
    # The soak's own blind spot, reported whether or not anything failed. A run
    # that excuses most of its own wall-clock cannot find much, and that should
    # be readable in the ledger rather than discovered by replaying a run.
    logger.info(
        "Excused by an outage: %.1f%% of wall-clock for a publish, %.1f%% for an RPC "
        "(%d outages over %.1fs). A leak outside that is reported; one inside is not.",
        100 * coverage["excused_fraction_publish"],
        100 * coverage["excused_fraction_rpc"],
        coverage["outage_count"],
        coverage["run_seconds"],
    )
    for name, share in coverage.get("phases", {}).items():
        logger.info(
            "  %s phase, %.1fs: %.1f%% excused for a publish, %.1f%% for an RPC "
            "(%d outages, %d compound)",
            name,
            share["seconds"],
            100 * share["excused_fraction_publish"],
            100 * share["excused_fraction_rpc"],
            share["outage_count"],
            share["compound_outages"],
        )

    if result["record_incomplete"]:
        logger.error(
            "Cyanide dropped %d injections from its bounded record, so a message with no "
            "recorded injection may simply have been forgotten. Refusing to report leaks "
            "from a record that is known to be incomplete; drain more often or raise "
            "injection_record_limit.",
            result["injections_dropped"],
        )
        sys.exit(1)

    if result["unexplained_count"] > 0:
        logger.error(
            "Leaks detected: %d messages reached no handler and no injected fault explains them.",
            result["unexplained_count"],
        )
        sys.exit(1)

    logger.info("Success! Every undelivered message is attributed to an injected fault.")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())

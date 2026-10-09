"""Refuse to judge a duration on a host too busy for the answer to mean anything.

A handful of assertions in this suite are genuinely about elapsed time -- how
long twenty concurrent probes take, for instance -- and cannot be rewritten as
ordering or counting without measuring something else. On a runner shared with
everything else on its host, those cross their ceiling because the machine was
busy, and the failure names a duration while the cause is contention.

THE THIRD OUTCOME. `scripts/check_benchmark_regression.py` answers this with a
refusal rather than a pass or a failure: three outcomes, because they call for
opposite responses -- read your diff, re-run on a quiet host, ship it. This is
the same reading and the same limit, expressed as a skip so a timing assertion
is never a false red and never a false green.

The limit is deliberately the same arithmetic as that script's, and
`tests/repo/test_the_load_refusals_agree.py` asserts the two have not drifted.
A second opinion about what "too busy" means would be worse than either.

WHY IT LIVES IN THE SHIPPED HELPERS. Two call sites need it: one in the unit
tier and one in a package's own tests. It was first written as
`tests/host_load.py`, and importing that from a package test **fails depending
on what else is in the run** -- `No module named 'tests.host_load'` when the
package's file is collected on its own, and fine when `tests/` is collected
alongside it. An order-dependent import is worse than either outcome.
`cliffracer.testing` is the one path both can rely on, and a caller writing a
latency test against this library needs the same refusal for the same reason.
"""

from __future__ import annotations

import os

# The one-minute load above which a duration is not judged. Same arithmetic as
# the benchmark gate: twice a reference that is never taken below 1.0, because
# twice almost-nothing is still almost-nothing and would refuse quiet runs.
LOAD_HEADROOM_MULTIPLE = 2.0
LOAD_REFERENCE_FLOOR = 1.0
LOAD_LIMIT = LOAD_HEADROOM_MULTIPLE * LOAD_REFERENCE_FLOOR


def one_minute_load() -> float | None:
    """This host's one-minute load, or None where the platform has none."""
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):  # pragma: no cover - not POSIX
        return None


def skip_if_the_host_is_too_busy_to_judge(what: str) -> None:
    """Skip, naming the load, when a duration cannot be judged here.

    `what` names the measurement, so the skip line says which assertion was not
    made rather than only that one was not.

    A host with no load average is judged: refusing there would skip the
    assertion permanently on any platform without `getloadavg`, which is a
    silent hole rather than a caveat.
    """
    load = one_minute_load()
    if load is None or load <= LOAD_LIMIT:
        return

    # Imported here, not at module scope. This module is reachable from
    # `import cliffracer.testing`, which is shipped, and pytest is a development
    # dependency -- a top-level import made that whole namespace require it, and
    # `import cliffracer.testing` raised ModuleNotFoundError without it.
    import pytest

    pytest.skip(
        f"NOT JUDGED: {what} on a host at 1-min load {load:.2f}, over the limit "
        f"of {LOAD_LIMIT:.2f} ({LOAD_HEADROOM_MULTIPLE:g}x a reference of at "
        f"least {LOAD_REFERENCE_FLOOR:g}). This is NOT a pass and NOT a failure: "
        f"the measurement was not taken. Every duration on a contended host "
        f"moves toward slower at once, and the ones that cross a ceiling are the "
        f"ones most sensitive to contention rather than the ones the code "
        f"changed. Re-run on a quiet host."
    )

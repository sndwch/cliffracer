"""The harness every seeded property check shares.

A property check generates cases from a seed, runs each through the code under test, and turns
every case that breaks the invariant into a `Finding`. A finding is allowed only when a named
`Limit` covers it: a known, stated behaviour with a reason. `assert_only_known_limits` fails on
any finding no limit covers, printing what reproduces it and the command that runs its seed again,
and reports a limit that matched nothing in the run as possibly stale. `assert_matches_only` holds a
limit's pinned example to that limit and no other. `assert_control_finds` is for each check's
CONTROL, which runs the same generator against a deliberately broken subject and must find
violations, or the property could not fail.

CI runs each check on its fixed seed and case count. `CLIFFRACER_PROPERTY_SEEDS` runs other seeds,
as a count starting at the fixed one (`5`) or as a list (`11,42`, or `20261003,` for one seed), and
`CLIFFRACER_PROPERTY_SCALE` multiplies the case counts. A value that names no seed, or no positive
finite scale, is refused by the variable's name. With either set, a stale limit fails instead of
warning: over many seeds a limit that matches nothing is no longer explained by one seed missing a
rare case.
"""

from __future__ import annotations

import math
import os
import warnings
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

SEEDS_VARIABLE = "CLIFFRACER_PROPERTY_SEEDS"
SCALE_VARIABLE = "CLIFFRACER_PROPERTY_SCALE"

#: How many uncovered findings a failure prints in full.
SHOWN = 3


class StaleLimitWarning(UserWarning):
    """A limit matched no finding in this run."""


def seeds(fixed: int) -> list[int]:
    """The seeds to run: `fixed` alone, or what `CLIFFRACER_PROPERTY_SEEDS` names. A whole number
    is a count of seeds starting at `fixed`; a value holding a comma is a list of seeds, so a single
    seed is written with a trailing comma (`20261003,`)."""
    raw = os.environ.get(SEEDS_VARIABLE, "").strip()
    if not raw:
        return [fixed]
    if "," not in raw:
        try:
            count = int(raw)
        except ValueError:
            raise ValueError(
                f"{SEEDS_VARIABLE}={raw!r}: give a count of seeds (5) or a comma-separated list of "
                "seeds (11,42, or 20261003, for one)"
            ) from None
        if count < 1:
            raise ValueError(f"{SEEDS_VARIABLE}={raw!r}: a count of seeds must be at least 1")
        return [fixed + offset for offset in range(count)]
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        raise ValueError(f"{SEEDS_VARIABLE}={raw!r}: the list names no seed")
    try:
        return [int(part) for part in parts]
    except ValueError:
        raise ValueError(
            f"{SEEDS_VARIABLE}={raw!r}: every seed in the list must be a whole number"
        ) from None


def cases(fixed: int) -> int:
    """`fixed` times `CLIFFRACER_PROPERTY_SCALE`, a positive finite number (1 when unset), at least 1."""
    raw = os.environ.get(SCALE_VARIABLE, "").strip()
    if not raw:
        return fixed
    try:
        scale = float(raw)
    except ValueError:
        scale = math.nan
    if not (math.isfinite(scale) and scale > 0):
        raise ValueError(f"{SCALE_VARIABLE}={raw!r}: the scale must be a positive finite number")
    return max(1, round(fixed * scale))


def widened() -> bool:
    """Whether this run was given other seeds or case counts than CI's fixed ones."""
    return bool(
        os.environ.get(SEEDS_VARIABLE, "").strip() or os.environ.get(SCALE_VARIABLE, "").strip()
    )


def rerun(seed: int) -> str:
    """The setting that runs `seed` alone: a one-item list, since a bare number is a count."""
    return f"{SEEDS_VARIABLE}={seed},"


@dataclass(frozen=True)
class Finding:
    """One case that breaks a check's invariant.

    `what` says in one line what went wrong; `reproduction` is what reproduces it, the generated
    source or input, printed verbatim on a failure; `detail` is what a `Limit` looks at.
    """

    seed: int
    index: int
    what: str
    reproduction: str
    detail: Any = None


@dataclass(frozen=True)
class Limit:
    """A known, stated behaviour that a finding is allowed to be, and why."""

    name: str
    reason: str
    covers: Callable[[Finding], bool]


@dataclass(frozen=True)
class Verdict:
    """How a run's findings fell: how many each limit matched (a finding counts for every limit
    that covers it), which findings no limit covered, and which limits matched nothing."""

    matched: dict[str, int]
    uncovered: list[Finding]
    stale: list[str]


def judge(findings: Iterable[Finding], limits: Sequence[Limit]) -> Verdict:
    """Every limit each finding matches is recorded; a finding none matches is uncovered."""
    matched = {limit.name: 0 for limit in limits}
    uncovered: list[Finding] = []
    for finding in findings:
        covering = [limit.name for limit in limits if limit.covers(finding)]
        for name in covering:
            matched[name] += 1
        if not covering:
            uncovered.append(finding)
    return Verdict(matched, uncovered, [name for name, count in matched.items() if count == 0])


def _test_node() -> str:
    """The running test's node id, from pytest's own variable, or nothing outside a test."""
    return os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0]


def assert_only_known_limits(
    findings: Iterable[Finding], limits: Sequence[Limit], *, check: str
) -> Verdict:
    """Fail when a finding is covered by no limit; report a limit that matched nothing.

    The failure prints, for the first `SHOWN` uncovered findings, the seed, the case index, what
    went wrong and the reproduction verbatim, each with the command that runs that seed alone.
    """
    verdict = judge(findings, limits)
    if verdict.uncovered:
        node = _test_node()
        shown = "\n\n".join(
            f"seed {f.seed}, case {f.index}: {f.what}\n"
            f"run it again: {rerun(f.seed)} pytest {node}\n{f.reproduction}"
            for f in verdict.uncovered[:SHOWN]
        )
        raise AssertionError(
            f"{check}: {len(verdict.uncovered)} case(s) break the invariant and no known limit "
            f"covers them (limits: {', '.join(limit.name for limit in limits) or 'none'}).\n\n"
            f"{shown}"
        )
    if verdict.stale:
        message = (
            f"{check}: these limits matched no case in this run and may be stale: "
            f"{', '.join(verdict.stale)}"
        )
        if widened():
            raise AssertionError(message)
        warnings.warn(message, StaleLimitWarning, stacklevel=2)
    return verdict


def assert_matches_only(finding: Finding, name: str, limits: Sequence[Limit]) -> None:
    """Hold a limit's pinned example to that limit: it matches the limit named `name` and no other,
    so two limits cannot quietly cover the same cases."""
    matching = [limit.name for limit in limits if limit.covers(finding)]
    if matching != [name]:
        raise AssertionError(
            f"the pinned example for {name!r} matches {matching or 'no limit'}, not {name!r} alone"
        )


def assert_control_finds(findings: Iterable[Finding], *, at_least: int, control: str) -> int:
    """Fail when a CONTROL, the same generator against a deliberately broken subject, finds fewer
    than `at_least` violations: the check could then not fail, and its passing would prove nothing.
    The CONTROL itself asserts that its broken subject is the one the generator reached."""
    found = sum(1 for _ in findings)
    if found < at_least:
        raise AssertionError(
            f"CONTROL {control}: found {found} violation(s), fewer than {at_least}; the check "
            "cannot be shown to fail, so its passing proves nothing"
        )
    return found

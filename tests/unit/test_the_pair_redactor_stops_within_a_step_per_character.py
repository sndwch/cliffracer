"""The query and fragment redactor ends within one step per character, whatever its loop does.

Each step of `_redact_pairs` moves past at least one character, so it returns within
`len(pairs) + 1` steps. A change that stopped it moving on (its end test removed, say) would loop
without end, growing the list it builds, until the memory cap on the run killed it and no failure
was recorded. The bound turns that into a named error. Both cases run in a child process, which
caps its own memory, so the copy without an end cannot take the host's.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cliffracer.core.endpoints import _redact_pairs, redact_nats_url

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
#: The child imports cliffracer and runs one call; an unbounded copy is cut off at its memory cap
#: first, so this is only reached if neither the bound nor the cap holds.
COMPLETES_WITHIN = 30


def _run(scenario: str) -> str:
    try:
        done = subprocess.run(
            [sys.executable, "-m", "tests.fixtures.redact_pairs_process", scenario],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=COMPLETES_WITHIN,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"_redact_pairs ({scenario}) did not end within {COMPLETES_WITHIN}s")
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_a_redactor_loop_that_never_reaches_its_end_fails_by_name():
    said = _run("never_ends")
    assert said.startswith("RAISED RuntimeError "), said
    assert "_redact_pairs did not reach the end of 14 characters in 15 steps" in said, said


def test_CONTROL_the_redactor_as_written_returns_the_pairs_redacted():
    assert _run("as_written") == "RETURNED a=1&password=***"


@pytest.mark.parametrize("pairs", ["", "&", "&&&"])
def test_the_inputs_that_need_every_step_of_the_bound_return_within_it(pairs: str):
    """An empty string, or one made only of separators, takes `len(pairs) + 1` steps: one per
    separator, and a last one that finds no pair after the end. A bound one step shorter would
    turn these into errors."""
    assert _redact_pairs(pairs) == pairs


def test_a_url_whose_query_ends_at_a_separator_is_printed_not_replaced():
    assert redact_nats_url("nats://h:4222/?&") == "nats://h:4222/?&"

"""One decision about whether a run is prefixed, and every name follows it.

`decided_prefix()` honours the opt-out. Three things that PRODUCE names did not
consult it -- they read `$CLIFFRACER_SUBJECT_PREFIX` directly:

    prefixed_name          tests/broker_isolation.py
    prefixed_subject       tests/broker_isolation.py
    ServiceConfig.subject_prefix   defaults from the same variable

So a caller who had exported a prefix and then set `CLIFFRACER_TEST_ISOLATE=0`
got the decision they asked for and the names they did not:

    decided_prefix()               -> None      the opt-out honoured
    prefixed_name('LIVE82')        -> tzzz9_LIVE82
    prefixed_subject('a')          -> tzzz9.a
    ServiceConfig().subject_prefix -> tzzz9

THE REMEDY IS THE FIXTURE, NOT THE HELPERS, and the issue's preferred one is
the dangerous half. Making a helper call `decided_prefix()` per name looks like
the tidier fix, but with no exported prefix that function generates one from
`session_prefix()`, whose seed carries `int(time.time())`. Measured a second
apart:

    decided_prefix()  ->  tb1c79cm
    decided_prefix()  ->  t05ec1dm

Two names in one run would carry different prefixes. The decision has to live in
one place that PUBLISHES its answer, which is what the session fixture does --
so under the opt-out it clears the variable, and every reader then agrees
because they are all reading the same absent thing.

WHY BOTH MODES ARE ASSERTED. "The opt-out gives bare names" is satisfied by a
helper that always gives bare names, which would break every isolated run. The
isolated case is what stops that being the fix.
"""

from __future__ import annotations

import os

import pytest

from cliffracer import ServiceConfig
from tests.broker_isolation import (
    PREFIX_ENV,
    decided_prefix,
    prefixed_name,
    prefixed_subject,
)

pytestmark = pytest.mark.unit

ISOLATE_ENV = "CLIFFRACER_TEST_ISOLATE"


def _names() -> dict[str, object]:
    """Every answer to "is this run prefixed", read the way its callers read it."""
    return {
        "decision": decided_prefix(),
        "name": prefixed_name("LIVE82"),
        "subject": prefixed_subject("events.a"),
        "config": ServiceConfig(name="svc").subject_prefix,
    }


def test_the_opt_out_gives_unprefixed_names(monkeypatch):
    """The reported defect: a stale exported prefix survived the opt-out."""
    monkeypatch.setenv(PREFIX_ENV, "tzzz9")
    monkeypatch.setenv(ISOLATE_ENV, "0")
    # what the session fixture does on the opt-out path
    monkeypatch.delenv(PREFIX_ENV, raising=False)

    answers = _names()

    assert answers == {
        "decision": None,
        "name": "LIVE82",
        "subject": "events.a",
        "config": None,
    }, answers


def test_CONTROL_an_isolated_run_still_prefixes_every_name(monkeypatch):
    """Otherwise "always bare" satisfies the test above and breaks every run."""
    monkeypatch.setenv(PREFIX_ENV, "tzzz9")
    monkeypatch.delenv(ISOLATE_ENV, raising=False)

    answers = _names()

    assert answers == {
        "decision": "tzzz9",
        "name": "tzzz9_LIVE82",
        "subject": "tzzz9.events.a",
        "config": "tzzz9",
    }, answers


def test_the_four_answers_agree_in_both_modes(monkeypatch):
    """The property, stated once: whatever the decision is, the names follow it.

    Asserted as a relation rather than as two lists of expected strings, so a
    fifth reader added later is checked by adding it to `_names` alone.
    """
    for isolate, cleared in ((None, False), ("0", True)):
        monkeypatch.setenv(PREFIX_ENV, "tzzz9")
        if isolate is None:
            monkeypatch.delenv(ISOLATE_ENV, raising=False)
        else:
            monkeypatch.setenv(ISOLATE_ENV, isolate)
        if cleared:
            monkeypatch.delenv(PREFIX_ENV, raising=False)

        answers = _names()
        decision = answers["decision"]

        assert answers["config"] == decision, answers
        if decision is None:
            assert answers["name"] == "LIVE82", answers
            assert answers["subject"] == "events.a", answers
        else:
            assert answers["name"] == f"{decision}_LIVE82", answers
            assert answers["subject"] == f"{decision}.events.a", answers


def test_CONTROL_the_decision_is_not_stable_when_it_generates_one(monkeypatch):
    """Why the helpers must NOT call `decided_prefix()` per name.

    This is the measurement that chose the remedy. `session_prefix()` seeds from
    `int(time.time())`, so the generated prefix changes across a second
    boundary. If this ever becomes stable, the cheaper remedy the issue
    preferred becomes available and this test should be revisited rather than
    deleted.
    """
    import time

    import tests.broker_isolation as isolation

    monkeypatch.delenv(PREFIX_ENV, raising=False)
    monkeypatch.delenv(ISOLATE_ENV, raising=False)

    seen = []
    for moment in (1_700_000_000.0, 1_700_000_001.0):
        monkeypatch.setattr(time, "time", lambda moment=moment: moment)
        seen.append(isolation.decided_prefix())

    assert seen[0] != seen[1], (
        f"decided_prefix() is now stable across a second boundary ({seen}); the "
        f"reason the helpers read a published answer instead of calling it may "
        f"no longer hold"
    )


def test_the_session_fixture_is_what_clears_it(monkeypatch):
    """The fix itself, driven rather than simulated.

    THE FOUR TESTS ABOVE DO NOT FENCE IT. They call `monkeypatch.delenv` to
    stand in for what the fixture does, so they assert the property GIVEN the
    clearing and pass with the clearing reverted -- measured, not assumed. A
    test that performs the remedy it is checking cannot observe its absence,
    which is the same shape as a probe that retrieves the exception it is
    looking for.

    So this one runs `_broker_namespace`'s own generator and asks what the
    environment looks like inside the yield.
    """
    import tests.conftest as conftest

    fixture = conftest._broker_namespace.__wrapped__

    monkeypatch.setenv(PREFIX_ENV, "tzzz9")
    monkeypatch.setenv(ISOLATE_ENV, "0")

    generator = fixture()
    try:
        handed_out = next(generator)
        inside = os.environ.get(PREFIX_ENV)
    finally:
        generator.close()

    assert handed_out is None, handed_out
    assert inside is None, (
        f"the fixture left {PREFIX_ENV}={inside!r} in place under the opt-out, so "
        f"prefixed_name, prefixed_subject and ServiceConfig still prefix"
    )


def test_CONTROL_an_isolated_session_still_has_the_variable(monkeypatch):
    """So "always clears it" is not what the test above is satisfied by."""
    import tests.conftest as conftest

    fixture = conftest._broker_namespace.__wrapped__

    monkeypatch.setenv(PREFIX_ENV, "tzzz9")
    monkeypatch.delenv(ISOLATE_ENV, raising=False)

    generator = fixture()
    try:
        handed_out = next(generator)
        inside = os.environ.get(PREFIX_ENV)
    finally:
        generator.close()

    assert handed_out == "tzzz9", handed_out
    assert inside == "tzzz9", inside


def test_the_inherited_value_is_put_back_afterwards(monkeypatch):
    """A fixture that clears an operator's variable must give it back.

    The opt-out is for one run, not for the shell it was launched from.
    """
    import tests.conftest as conftest

    fixture = conftest._broker_namespace.__wrapped__

    monkeypatch.setenv(PREFIX_ENV, "tzzz9")
    monkeypatch.setenv(ISOLATE_ENV, "0")

    generator = fixture()
    next(generator)
    generator.close()

    assert os.environ.get(PREFIX_ENV) == "tzzz9"

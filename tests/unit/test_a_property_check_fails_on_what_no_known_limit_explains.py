"""The shared property harness: seeds and case counts, known limits, and the CONTROL floor.

A seeded property check turns every case that breaks its invariant into a finding, and a finding is
allowed only when a named limit covers it. These tests hold the harness to that: a finding no limit
covers fails and prints what reproduces it with a command that runs its seed alone, a limit that
matched nothing is reported (and fails when the run was widened past CI's fixed seed), a pinned
example matches its own limit and no other, a setting that names no seed or no scale is refused by
name, and a CONTROL that finds too little fails by name.
"""

import re

import pytest

from tests.fixtures.properties import (
    SCALE_VARIABLE,
    SEEDS_VARIABLE,
    Finding,
    Limit,
    StaleLimitWarning,
    assert_control_finds,
    assert_matches_only,
    assert_only_known_limits,
    cases,
    judge,
    seeds,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fixed_run(monkeypatch):
    """CI's run: neither variable set."""
    monkeypatch.delenv(SEEDS_VARIABLE, raising=False)
    monkeypatch.delenv(SCALE_VARIABLE, raising=False)


def finding(index: int, kind: str, seed: int = 7) -> Finding:
    return Finding(
        seed=seed,
        index=index,
        what=f"a {kind} cell",
        reproduction=f"class Case{index}: ...",
        detail=kind,
    )


LOST = Limit("lost-by-design", "a stated limit", lambda f: f.detail == "lost")
OTHER = Limit("other-by-design", "another stated limit", lambda f: f.detail == "other")
ANY = Limit("anything", "covers every finding", lambda f: True)


def test_ci_runs_the_fixed_seed_and_case_count():
    assert seeds(7) == [7]
    assert cases(1500) == 1500


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3", [7, 8, 9]),
        ("1", [7]),
        ("11,42", [11, 42]),
        (" 5 , 6 ", [5, 6]),
        ("20261003,", [20261003]),
    ],
    ids=["a-count-from-the-fixed-seed", "a-count-of-one", "a-list", "a-spaced-list", "one-seed"],
)
def test_more_seeds_are_a_count_or_a_list(monkeypatch, value, expected):
    monkeypatch.setenv(SEEDS_VARIABLE, value)

    assert seeds(7) == expected


@pytest.mark.parametrize(
    "value",
    ["0", "-2", "abc", "3.5", ",", " , ", "1,a"],
    ids=[
        "zero",
        "negative",
        "a-word",
        "a-fraction",
        "a-bare-comma",
        "a-spaced-comma",
        "a-word-in-a-list",
    ],
)
def test_a_seed_setting_that_names_no_seed_is_refused_by_name(monkeypatch, value):
    monkeypatch.setenv(SEEDS_VARIABLE, value)

    with pytest.raises(ValueError, match=SEEDS_VARIABLE):
        seeds(7)


@pytest.mark.parametrize(("value", "expected"), [("4", 6000), ("0.5", 750), ("0.0001", 1)])
def test_the_scale_multiplies_the_case_count_and_keeps_at_least_one(monkeypatch, value, expected):
    monkeypatch.setenv(SCALE_VARIABLE, value)

    assert cases(1500) == expected


@pytest.mark.parametrize("value", ["0", "-1", "abc", "inf", "1e400", "nan"])
def test_a_scale_that_is_not_a_positive_finite_number_is_refused_by_name(monkeypatch, value):
    monkeypatch.setenv(SCALE_VARIABLE, value)

    with pytest.raises(ValueError, match=SCALE_VARIABLE):
        cases(1500)


def test_every_limit_a_finding_matches_is_recorded():
    verdict = judge([finding(0, "lost"), finding(1, "other"), finding(2, "lost")], [LOST, ANY])

    assert verdict.matched == {"lost-by-design": 2, "anything": 3}
    assert verdict.uncovered == []
    assert verdict.stale == []


@pytest.mark.parametrize("widen", [False, True], ids=["fixed-run", "widened-run"])
def test_a_limit_shadowed_by_another_but_still_matching_is_not_stale(monkeypatch, widen):
    """Two limits covering the same three findings: neither matched nothing."""
    if widen:
        monkeypatch.setenv(SEEDS_VARIABLE, "5")
    both = Limit("also-lost", "the same cases", lambda f: f.detail == "lost")

    verdict = assert_only_known_limits(
        [finding(i, "lost") for i in range(3)], [LOST, both], check="H wire"
    )

    assert verdict.matched == {"lost-by-design": 3, "also-lost": 3}
    assert verdict.stale == []


def test_findings_every_limit_covers_pass_and_are_counted():
    verdict = assert_only_known_limits(
        [finding(0, "lost"), finding(1, "other")], [LOST, OTHER], check="H wire"
    )

    assert verdict.matched == {"lost-by-design": 1, "other-by-design": 1}


def test_a_finding_no_limit_covers_fails_and_prints_what_reproduces_it():
    with pytest.raises(AssertionError) as failed:
        assert_only_known_limits(
            [finding(0, "lost"), finding(4, "silent change")], [LOST], check="H wire"
        )

    text = str(failed.value)
    assert "H wire: 1 case(s) break the invariant" in text
    assert "seed 7, case 4: a silent change cell" in text
    assert "class Case4: ..." in text
    assert "class Case0" not in text, "a covered finding is not a failure"


def test_the_printed_command_runs_the_failing_seed_and_no_other(monkeypatch):
    """A bare number is a count of seeds, so the command must name the seed as a list."""
    with pytest.raises(AssertionError) as failed:
        assert_only_known_limits([finding(0, "silent", seed=20261003)], [], check="U")
    setting = re.search(rf"{SEEDS_VARIABLE}=(\S+)", str(failed.value))
    assert setting, str(failed.value)

    monkeypatch.setenv(SEEDS_VARIABLE, setting.group(1))

    assert seeds(7) == [20261003]


def test_the_printed_command_names_the_running_test():
    with pytest.raises(AssertionError) as failed:
        assert_only_known_limits([finding(0, "silent")], [], check="U")

    assert "test_the_printed_command_names_the_running_test" in str(failed.value)


def test_only_the_first_few_uncovered_findings_are_printed_in_full():
    with pytest.raises(AssertionError) as failed:
        assert_only_known_limits([finding(i, "silent") for i in range(10)], [], check="U")

    text = str(failed.value)
    assert "10 case(s)" in text
    assert "case 2:" in text and "case 3:" not in text


def test_a_limit_that_matched_nothing_is_reported_in_a_fixed_run():
    with pytest.warns(StaleLimitWarning, match="other-by-design"):
        assert_only_known_limits([finding(0, "lost")], [LOST, OTHER], check="H wire")


@pytest.mark.parametrize("variable", [SEEDS_VARIABLE, SCALE_VARIABLE])
def test_a_limit_that_matched_nothing_fails_once_the_run_is_widened(monkeypatch, variable):
    monkeypatch.setenv(variable, "2")

    with pytest.raises(AssertionError, match="other-by-design"):
        assert_only_known_limits([finding(0, "lost")], [LOST, OTHER], check="H wire")


def test_a_pinned_example_that_matches_its_own_limit_alone_passes():
    assert_matches_only(finding(0, "lost"), "lost-by-design", [LOST, OTHER])


@pytest.mark.parametrize(
    ("example", "limits"),
    [(finding(0, "lost"), [LOST, ANY]), (finding(0, "lost"), [OTHER])],
    ids=["matched-by-another-limit-too", "matched-by-none"],
)
def test_a_pinned_example_that_matches_another_limit_or_none_fails(example, limits):
    with pytest.raises(AssertionError, match="lost-by-design"):
        assert_matches_only(example, "lost-by-design", limits)


def test_a_control_that_finds_enough_returns_its_count():
    found = [finding(i, "lost") for i in range(30)]

    assert assert_control_finds(found, at_least=25, control="alias dump") == 30
    assert assert_control_finds(found[:25], at_least=25, control="alias dump") == 25


def test_a_control_that_finds_too_little_fails_by_name():
    with pytest.raises(AssertionError, match="CONTROL alias dump: found 3 violation"):
        assert_control_finds(
            [finding(i, "lost") for i in range(3)], at_least=25, control="alias dump"
        )


def test_CONTROL_a_control_that_finds_nothing_fails():
    """A generator the subject's breakage never reaches is the case the floor exists for."""
    with pytest.raises(AssertionError, match="found 0 violation"):
        assert_control_finds([], at_least=1, control="raw URL")

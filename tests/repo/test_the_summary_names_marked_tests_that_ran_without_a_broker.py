"""A `nats_required` test that RAN with no broker reachable is named by the terminal summary.

The summary used to report only how many marked tests the collection hook held back, and that
number is zero for a healthy run with a broker and just as zero for a run whose hook never ran, or
never matched: the marked tests then run against whatever answers at the default address, and the
line still reads as though nothing was dialled. What settles it is the marked tests that RAN. With
no broker reachable there should be none, so the summary now prints how many ran and, when the
broker was not reachable and some did, says so.

Each case is a real child `pytest`: what is under test is `pytest_terminal_summary` fed by the real
collection hook, and a call into one function would test a copy of the hook's input. The hook is
"broken" by a plugin that undoes the skips it added, which is what a hook that did not run leaves
behind. The broker is a counting listener the test owns, so nothing here can reach a real one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import conftest
from tests.repo.test_a_plain_pytest_does_not_dial_a_broker_nobody_named import (
    A_BROKER_TEST_FILE,
    _CountingListener,
    _run_pytest,
)

pytestmark = pytest.mark.repo

BREAK_THE_HOOK = "skip_hook_is_broken"
WARNING = "nats_required test(s) RAN although no broker was reachable"


def _break_the_skip_hook(tmp_path) -> None:
    (tmp_path / f"{BREAK_THE_HOOK}.py").write_text(
        "import pytest\n\n"
        "@pytest.hookimpl(trylast=True)\n"
        "def pytest_collection_modifyitems(config, items):\n"
        "    for item in items:\n"
        "        item.own_markers[:] = [m for m in item.own_markers if m.name != 'skip']\n"
        "    config._nats_skipped = []\n"
    )


def _summary(result) -> str:
    return next(line for line in result.stdout.splitlines() if "nats:" in line and " ran," in line)


def test_a_marked_test_that_ran_with_no_broker_reachable_is_named(tmp_path):
    _break_the_skip_hook(tmp_path)
    with _CountingListener() as listener:
        result = _run_pytest(
            listener,
            tmp_path,
            [A_BROKER_TEST_FILE, "-p", BREAK_THE_HOOK, "-m", "nats_required", "--timeout=60"],
            name_the_broker=False,
        )

    out = result.stdout
    assert listener.dials >= 1, "the premise: with the hook broken the marked test did dial\n" + out
    assert WARNING in out, out
    assert "no broker named, not dialling" in _summary(result), _summary(result)
    assert "nats_required: 0 ran" not in _summary(result), _summary(result)
    assert "held back by the nats_required marker" not in out, out


def test_a_healthy_run_with_no_broker_holds_them_back_and_raises_no_warning(tmp_path):
    with _CountingListener() as listener:
        result = _run_pytest(
            listener, tmp_path, [A_BROKER_TEST_FILE, "-m", "nats_required"], name_the_broker=False
        )

    assert listener.dials == 0, result.stdout
    assert WARNING not in result.stdout, result.stdout
    assert "nats_required: 0 ran" in _summary(result), _summary(result)
    assert "held back by the nats_required marker" in result.stdout, result.stdout


def test_CONTROL_with_a_broker_named_the_marked_tests_run_and_that_is_not_a_warning(tmp_path):
    with _CountingListener() as listener:
        result = _run_pytest(
            listener,
            tmp_path,
            [A_BROKER_TEST_FILE, "-m", "nats_required", "--timeout=60"],
            name_the_broker=True,
        )

    assert listener.dials >= 1, result.stdout
    assert WARNING not in result.stdout, result.stdout
    assert "nats_required: 0 ran" not in _summary(result), _summary(result)


def _fake_reporter(passed=(), failed=(), skipped=()):
    lines: list[str] = []

    def report(nodeid, marked):
        return SimpleNamespace(nodeid=nodeid, keywords={"nats_required": 1} if marked else {})

    stats = {
        "passed": [report(n, m) for n, m in passed],
        "failed": [report(n, m) for n, m in failed],
        "skipped": [report(n, m) for n, m in skipped],
    }
    reporter = SimpleNamespace(
        stats=stats,
        write_sep=lambda sep, text: lines.append(text),
        write_line=lambda text, **kw: lines.append(text),
    )
    return reporter, lines


def _config(reachable):
    return SimpleNamespace(
        _nats_url="nats://nowhere:1",
        _nats_reachable=reachable,
        _nats_asked_for=None,
        _nats_skipped=[],
    )


@pytest.mark.parametrize(
    ("reachable", "warns"),
    [(False, True), (True, False), (None, False)],
    ids=["no", "yes", "unknown"],
)
def test_the_warning_needs_both_a_marked_test_that_ran_and_no_reachable_broker(reachable, warns):
    reporter, lines = _fake_reporter(passed=[("a::marked", True), ("a::plain", False)])

    conftest.pytest_terminal_summary(reporter, 0, _config(reachable))

    assert any(WARNING in text for text in lines) is warns, lines
    assert "nats_required: 1 ran" in lines[0]


def test_a_marked_test_that_failed_or_errored_without_a_broker_counts_as_having_run():
    reporter, lines = _fake_reporter(failed=[("a::failed", True)])

    conftest.pytest_terminal_summary(reporter, 1, _config(False))

    assert any(WARNING in text and "a::failed" in text for text in lines), lines


def test_a_skipped_marked_test_did_not_run_and_is_not_named():
    reporter, lines = _fake_reporter(skipped=[("a::held", True)])

    conftest.pytest_terminal_summary(reporter, 0, _config(False))

    assert not any(WARNING in text for text in lines), lines
    assert "nats_required: 0 ran" in lines[0]

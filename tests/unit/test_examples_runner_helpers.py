"""Tests for examples runner helper logic."""

import ast
import subprocess
import sys

import pytest

from tests.integration.test_examples_run import (
    _BOOTSTRAP,
    _CRASH_EXCERPT,
    EXAMPLES,
    RUNNABLE,
    SKIP,
    crashed_services,
    runnable_reason,
)

pytestmark = pytest.mark.unit


# --- the crash reader ------------------------------------------------------


def test_a_crash_line_is_detected():
    """Against the orchestrator's real format, not a paraphrase.

    `runners/orchestrator.py:129` logs `f"Service crashed: {e}"`; a reader
    tuned to a paraphrase passes on prose nobody logs.
    """
    output = (
        "2026-09-07 14:00:00.000 | INFO     | ...orchestrator:_run:110 - "
        "Starting service 'order_service' (attempt #1)\n"
        "2026-09-07 14:00:01.000 | ERROR    | ...orchestrator:_run:129 - "
        "Service crashed: 'OrderService' object has no attribute 'post'\n"
    )
    found = crashed_services(output)
    assert found == ["Service crashed: 'OrderService' object has no attribute 'post'"], found


def test_a_clean_run_has_no_crash_lines():
    """The other half: a reader returning every line would pass the test above
    and fail every example."""
    output = (
        "2026-09-07 14:00:00.000 | INFO | Starting service 'order_service' (attempt #1)\n"
        "2026-09-07 14:00:00.100 | SUCCESS | Service 'order_service' started\n"
        "Running 5 services\n"
    )
    assert crashed_services(output) == []


def test_a_json_logging_example_reports_one_short_line():
    """The real shape: crash and whole traceback on ONE physical line, once per
    restart. Raw, the ecommerce crash was ~4 KB per attempt and three attempts
    made an assertion message no one would read to the end."""
    blob = (
        "{\"text\": \"Service crashed: 'OrderService' object has no attribute 'post'"
        "\\nTraceback (most recent call last):\\n" + "x" * 4000 + '"}'
    )
    found = crashed_services(blob + "\n" + blob + "\n")
    assert found == ["Service crashed: 'OrderService' object has no attribute 'post'   [x2]"], found
    assert len(found[0]) < 200, len(found[0])


def test_a_crash_with_no_escaped_newline_is_cut_at_the_excerpt_cap():
    """The cap bounds the line the newline cut cannot: a crash whose whole 4 KB message is on one
    physical line, as `ValueError(<a repr>)` is. The test above ends at an escaped newline, so it
    would pass for any cap, or none."""
    line = "2026-09-07 14:00:01.000 | ERROR | Service crashed: ValueError(" + "y" * 4000 + ")\n"

    (found,) = crashed_services(line)

    assert found == ("Service crashed: ValueError(" + "y" * 4000)[:_CRASH_EXCERPT]
    assert len(found) == _CRASH_EXCERPT == 160


def test_the_cap_is_counted_from_the_marker_and_not_from_the_start_of_the_line():
    """A long prefix (a timestamp, a logger name) is not part of the excerpt."""
    line = "x" * 500 + " Service crashed: " + "z" * 500 + "\n"

    (found,) = crashed_services(line)

    assert found.startswith("Service crashed: ") and len(found) == _CRASH_EXCERPT


def test_a_crash_shorter_than_the_cap_is_returned_whole():
    (found,) = crashed_services("... Service crashed: short reason\n")

    assert found == "Service crashed: short reason"


def test_two_long_crashes_that_agree_to_the_cap_are_one_line():
    """Deduping is on the excerpt, so crashes identical for their first 160 characters are the
    same crash however they end, which is what keeps a restart loop to one line."""
    same = "Service crashed: " + "p" * 400
    output = f"a {same}A\n" + f"b {same}B\n"

    assert len(crashed_services(output)) == 1 and crashed_services(output)[0].endswith("[x2]")


def _crash(reason: str) -> str:
    return f"2026-09-07 14:00:01.000 | ERROR    | ...orchestrator:_run:129 - Service crashed: {reason}\n"


def test_two_distinct_crashes_are_reported_once_each_in_the_order_first_seen():
    """The half of the contract a single crash cannot show: per DISTINCT crash, first-seen order.

    Z, A, Z: the first line is the first failure even though it sorts last, a repeat of Z counts into
    Z's line and does not move it after A, and A stays a line of its own.
    """
    output = _crash("zulu broke") + _crash("alpha broke") + _crash("zulu broke")

    assert crashed_services(output) == [
        "Service crashed: zulu broke   [x2]",
        "Service crashed: alpha broke",
    ]


def test_crashes_that_differ_only_a_little_stay_separate_lines():
    """Near-identical is not identical: deduping on a prefix, or on the service, would merge them."""
    output = _crash("'A' object has no attribute 'x'") + _crash("'A' object has no attribute 'y'")

    assert crashed_services(output) == [
        "Service crashed: 'A' object has no attribute 'x'",
        "Service crashed: 'A' object has no attribute 'y'",
    ]


def test_three_distinct_crashes_keep_their_order_when_the_later_ones_repeat():
    """Order is by first sight and not by count: the most repeated crash is not moved to the top."""
    output = (
        _crash("first")
        + _crash("second")
        + _crash("second")
        + _crash("second")
        + _crash("third")
        + _crash("third")
    )

    assert crashed_services(output) == [
        "Service crashed: first",
        "Service crashed: second   [x3]",
        "Service crashed: third   [x2]",
    ]


# --- the __main__ rule -------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        'if __name__ == "__main__":\n    pass\n',
        'if "__main__" == __name__:\n    pass\n',
    ],
)
def test_a_main_block_makes_a_file_runnable(source):
    """Both orders, because the rule is structural rather than textual."""
    assert runnable_reason(ast.parse(source)) == "has an __main__ block"


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("def main():\n    pass\n", "defines main()"),
        ("async def main():\n    pass\n", "defines main()"),
        ("class Service:\n    pass\n", "defines a class"),
        ("class Service:\n    def main(self):\n        pass\n", "defines a class"),
        ("def build():\n    class Inner:\n        pass\n", None),
        # A class beside a __main__ block reports the class, and a main() beside a class reports
        # main(): the three are tried in that order, so the order is part of what is asserted.
        ('class Service:\n    pass\nif __name__ == "__main__":\n    pass\n', "defines a class"),
        ("class Service:\n    pass\ndef main():\n    pass\n", "defines main()"),
        ('def main():\n    pass\nif __name__ == "__main__":\n    main()\n', "defines main()"),
        ('if __name__ == "__main__" == other:\n    pass\n', "has an __main__ block"),
    ],
    ids=[
        "def-main",
        "async-def-main",
        "class",
        "a-method-called-main-is-a-class",
        "a-class-inside-a-function-is-not-module-level",
        "class-before-main-block",
        "main-before-class",
        "main-before-main-block",
        "chained-comparison",
    ],
)
def test_the_reason_an_example_is_runnable_and_the_order_the_reasons_are_tried_in(source, reason):
    assert runnable_reason(ast.parse(source)) == reason


def test_a_module_with_nothing_to_run_is_not_runnable():
    """The half that stops the filter admitting everything.

    Nothing under examples/ is excluded today, so this synthetic input is the
    only thing holding the rule: without it, a `runnable_reason` that returned
    a string unconditionally would pass every other test here.
    """
    assert runnable_reason(ast.parse("X = 1\nY = X + 1\n")) is None


def test_a_main_guard_inside_a_function_does_not_count():
    """Only module level. A nested block does not run the file.

    Asked of `runnable_reason`, which is where the rule lives: it scans `tree.body`. The
    guard itself, `is_main_block`, says yes to any `if __name__ == "__main__"` it is handed, so
    asking it about the function that contains one only learns that a function is not an `if`.
    """
    source = 'def f():\n    if __name__ == "__main__":\n        pass\n'

    assert runnable_reason(ast.parse(source)) is None


def test_CONTROL_the_same_guard_at_module_level_does_count():
    """The nested case above passes for a `runnable_reason` that never reads a guard at all."""
    source = 'def f():\n    pass\nif __name__ == "__main__":\n    f()\n'

    assert runnable_reason(ast.parse(source)) == "has an __main__ block"


def test_run_simple_demo_is_collected():
    """Verify run_simple_demo.py is collected by runner discovery."""
    names = {p.relative_to(EXAMPLES).as_posix() for p in RUNNABLE}
    assert "ecommerce/run_simple_demo.py" in names, sorted(names)


# --- SKIP list validation --------------------------------------------------
#
# Each predicate lives in one function that both the real assertion and its
# control call. Written out twice, a change to the real expression would leave
# the control exercising the old copy and still passing.


def _entries_that_do_not_exist(entries):
    return [rel for rel in entries if not (EXAMPLES / rel).exists()]


def _entries_not_collected(entries, collected):
    return [rel for rel in entries if rel not in collected]


def test_CONTROL_every_skip_entry_still_exists():
    """A SKIP naming a deleted file silently stops skipping and hides nothing."""
    missing = _entries_that_do_not_exist(SKIP)
    assert not missing, f"SKIP names files that no longer exist: {missing}"

    synthetic_skip = {
        "ecommerce/run_simple_demo.py": "Valid existing example",
        "nonexistent/deleted_example.py": "Simulated missing example",
    }
    assert _entries_that_do_not_exist(synthetic_skip) == ["nonexistent/deleted_example.py"]


def test_CONTROL_every_skip_entry_is_collected():
    """Verify every entry in SKIP is discovered by the runner."""
    collected = {p.relative_to(EXAMPLES).as_posix() for p in RUNNABLE}
    inert = _entries_not_collected(SKIP, collected)
    assert not inert, (
        "SKIP names files the sweep does not collect, so these entries excuse "
        f"nothing and hide nothing: {inert}. Either the file has no main(), no "
        "class and no `__main__` block -- in which case it is not an example "
        "and does not belong in SKIP -- or `runnable_reason` no longer sees it."
    )

    synthetic_skip = {
        "ecommerce/run_simple_demo.py": "Collected runnable example",
        "nonexistent/inert_example.py": "Simulated uncollected example",
    }
    assert _entries_not_collected(synthetic_skip, collected) == ["nonexistent/inert_example.py"]


# --- child process sys.path ------------------------------------------------


def _what_an_example_sees(tmp_path, url):
    """Run `_BOOTSTRAP` for a one-line example that prints its default config."""
    example = tmp_path / "show.py"
    example.write_text(
        "from cliffracer import ServiceConfig\n"
        "config = ServiceConfig(name='shown')\n"
        "print(config.nats_url, config.health_port)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP, url, str(example)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.split()


def test_a_broker_url_handed_to_the_bootstrap_becomes_the_examples_default(tmp_path):
    """The branch a default run never takes: the integration tests pass an empty URL when no
    broker is configured, so `if url:` was unexecuted. With one, the example's own
    `ServiceConfig()` dials it, and it still asks the OS for its health port."""
    nats_url, health_port = _what_an_example_sees(tmp_path, "nats://example.invalid:4333")

    assert nats_url == "nats://example.invalid:4333"
    assert health_port == "0"


def test_CONTROL_with_no_broker_url_the_examples_own_default_stands(tmp_path):
    """The default is what a plain child process of this environment sees: not this process's,
    which the suite's conftest points at the run's broker."""
    plain = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cliffracer import ServiceConfig; print(ServiceConfig(name='x').nats_url)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert plain.returncode == 0, plain.stderr

    nats_url, health_port = _what_an_example_sees(tmp_path, "")

    assert nats_url == plain.stdout.strip()
    assert health_port == "0"


def test_an_example_can_import_a_sibling_module(tmp_path):
    """Verify examples can import sibling modules when executed by runner."""
    home = tmp_path / "example_dir"
    home.mkdir()
    (home / "sibling.py").write_text("VALUE = 'reached'\n")
    (home / "runs.py").write_text(
        "from sibling import VALUE\n\nif __name__ == '__main__':\n    print(VALUE)\n"
    )
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()

    result = subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP, "", str(home / "runs.py")],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reached" in result.stdout, result.stdout + result.stderr

"""Tests for examples runner helper logic."""

import ast
import subprocess
import sys

import pytest

from tests.integration.test_examples_run import (
    _BOOTSTRAP,
    EXAMPLES,
    RUNNABLE,
    SKIP,
    crashed_services,
    is_main_block,
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


def test_a_module_with_nothing_to_run_is_not_runnable():
    """The half that stops the filter admitting everything.

    Nothing under examples/ is excluded today, so this synthetic input is the
    only thing holding the rule: without it, a `runnable_reason` that returned
    a string unconditionally would pass every other test here.
    """
    assert runnable_reason(ast.parse("X = 1\nY = X + 1\n")) is None


def test_a_main_guard_inside_a_function_does_not_count():
    """Only module level. A nested block does not run the file."""
    source = 'def f():\n    if __name__ == "__main__":\n        pass\n'
    tree = ast.parse(source)
    assert not any(is_main_block(n) for n in tree.body)


def test_run_simple_demo_is_collected():
    """Verify run_simple_demo.py is collected by runner discovery."""
    names = {p.relative_to(EXAMPLES).as_posix() for p in RUNNABLE}
    assert "ecommerce/run_simple_demo.py" in names, sorted(names)


# --- SKIP list validation --------------------------------------------------


def test_CONTROL_every_skip_entry_still_exists():
    """A SKIP naming a deleted file silently stops skipping and hides nothing."""
    missing = [rel for rel in SKIP if not (EXAMPLES / rel).exists()]
    assert not missing, f"SKIP names files that no longer exist: {missing}"


def test_CONTROL_every_skip_entry_is_collected():
    """Verify every entry in SKIP is discovered by the runner."""
    collected = {p.relative_to(EXAMPLES).as_posix() for p in RUNNABLE}
    inert = [rel for rel in SKIP if rel not in collected]
    assert not inert, (
        "SKIP names files the sweep does not collect, so these entries excuse "
        f"nothing and hide nothing: {inert}. Either the file has no main(), no "
        "class and no `__main__` block -- in which case it is not an example "
        "and does not belong in SKIP -- or `runnable_reason` no longer sees it."
    )


# --- child process sys.path ------------------------------------------------


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

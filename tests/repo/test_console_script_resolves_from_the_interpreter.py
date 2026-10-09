"""A console script is found by the interpreter that runs the test, not by PATH.

`CLAUDE.md` documents two ways to run the suite. `uv run pytest` prepends
`.venv/bin` to `PATH`; `.venv/bin/python -m pytest` does not. Tests that
resolved the `cliffracer-generate-client` console script through `shutil.which`
therefore failed under the second one -- five in the integration tier and one
in the unit tier -- and said "console script not installed: run uv sync", which
is the wrong remedy: the script was installed all along, sitting in the very
`.venv/bin` beside the interpreter that was running the test.

Resolving it beside `sys.executable` fixes the documented invocation instead of
documenting around it, and is the better answer even where `PATH` would have
worked: with two virtualenvs in play, `PATH` can hand back a console script
from a different tree than the interpreter under test, which is the same
borrowed-tree hazard that makes a scratch worktree measure someone else's
source.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from conftest import console_script

pytestmark = pytest.mark.repo

SCRIPT = "cliffracer-generate-client"


def test_the_script_is_found_with_nothing_on_PATH(monkeypatch: pytest.MonkeyPatch):
    """The reported case: the documented direct invocation, reduced.

    An empty PATH is the strongest form of "`.venv/bin` is not on PATH", and it
    is what the five integration tests met.
    """
    monkeypatch.setenv("PATH", "")

    found = console_script(SCRIPT)

    assert Path(found).is_file(), found
    assert os.access(found, os.X_OK), f"{found} is not executable"
    assert Path(found).parent == Path(sys.executable).parent, (
        f"{found} did not come from the running interpreter's directory "
        f"({Path(sys.executable).parent})"
    )


def test_the_interpreters_copy_wins_over_a_different_one_on_PATH(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The discriminating control: PATH is not merely a fallback, it is second.

    Without this the resolver could be reading PATH first and passing the test
    above only because PATH was empty. A same-named executable elsewhere is the
    input that tells the two orders apart -- and it is not hypothetical, since
    this repository has several worktrees each with their own `.venv`.
    """
    impostor = tmp_path / SCRIPT
    impostor.write_text("#!/bin/sh\nexit 0\n")
    impostor.chmod(impostor.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path))

    found = console_script(SCRIPT)

    assert Path(found) != impostor, (
        "the script on PATH was preferred to the one beside the interpreter, so "
        "a second virtualenv on PATH decides which tree the test measures"
    )
    assert Path(found).parent == Path(sys.executable).parent, found


def test_a_script_that_is_absent_fails_naming_where_it_looked():
    """The other half. A resolver that never fails is not a resolver.

    The old message named a remedy that does nothing for a PATH miss; this one
    has to name both places, so the reader can tell "not installed" from "not
    on PATH" without reproducing the search by hand.
    """
    with pytest.raises(AssertionError) as caught:
        console_script("cliffracer-no-such-console-script")

    message = str(caught.value)
    assert "cliffracer-no-such-console-script" in message, message
    assert str(Path(sys.executable).parent) in message, (
        f"the failure does not say where beside the interpreter it looked:\n{message}"
    )
    assert "PATH" in message, message
    assert "uv sync" in message, (
        "the genuinely-absent case is the one where `uv sync` IS the remedy, so "
        f"it should still say so:\n{message}"
    )


def test_the_failure_separates_a_partial_sync_from_a_foreign_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """One message covered two causes with different remedies.

    A PATH miss no longer reaches the failure at all. What is left is absence,
    and a virtualenv holding the sibling scripts but not this one has had a
    partial or reverted sync -- `uv run` re-syncs from the lockfile and can undo
    a manual install, so a script goes missing between two runs with nobody
    syncing deliberately. That is a different fix from "you are not in a
    cliffracer environment", and the reader should not have to go and list the
    directory to find out which they have.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python").write_text("")
    monkeypatch.setattr(sys, "executable", str(fake_bin / "python"))
    monkeypatch.setenv("PATH", "")

    with pytest.raises(AssertionError) as empty:
        console_script(SCRIPT)
    assert "no cliffracer scripts at all" in str(empty.value), empty.value
    assert "not a cliffracer environment" in str(empty.value), empty.value

    sibling = fake_bin / "cliffracer"
    sibling.write_text("#!/bin/sh\nexit 0\n")
    sibling.chmod(sibling.stat().st_mode | stat.S_IEXEC)

    with pytest.raises(AssertionError) as partial:
        console_script(SCRIPT)
    message = str(partial.value)
    assert "cliffracer" in message, (
        f"the failure does not name the siblings it found, which is the reading "
        f"that tells a partial sync from a foreign environment:\n{message}"
    )
    assert "reverted sync" in message, message


def test_a_script_only_on_PATH_is_still_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """PATH stays a fallback rather than being dropped.

    A resolver that looked only beside the interpreter would break any
    environment that installs console scripts elsewhere -- a system package, or
    a wrapper on PATH with no venv at all.
    """
    name = "cliffracer-only-on-path"
    elsewhere = tmp_path / name
    elsewhere.write_text("#!/bin/sh\nexit 0\n")
    elsewhere.chmod(elsewhere.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert Path(console_script(name)) == elsewhere


def test_a_non_executable_file_beside_the_interpreter_is_not_the_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Present is not the same as runnable.

    Handing back a path the caller then fails to execute moves the error to a
    place that does not explain it.
    """
    name = "cliffracer-not-executable"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python").write_text("")
    dud = fake_bin / name
    dud.write_text("not runnable")
    dud.chmod(0o644)
    monkeypatch.setattr(sys, "executable", str(fake_bin / "python"))
    monkeypatch.setenv("PATH", "")

    with pytest.raises(AssertionError) as caught:
        console_script(name)
    assert name in str(caught.value)

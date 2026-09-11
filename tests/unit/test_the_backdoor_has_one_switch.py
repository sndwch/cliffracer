"""Names the debug console no longer carries are gone from code and docs.

`BackdoorConfig.enabled` -- in the constructor, or `CLIFFRACER_BACKDOOR_ENABLED`
in the environment -- is the switch. Two other spellings were documented and
neither was consulted: `CLIFFRACER_DISABLE_BACKDOOR` and `CLIFFRACER_ENV` were
read only by `is_backdoor_enabled`, which was exported and called by nothing.
An operator following the security guidance set one of them and believed a
remote-code-execution endpoint was off while `enabled=True` still opened it.

THIS IS THE TEST THAT IS RED BEFORE THE REMOVAL, and the behavioural pair in
`packages/cliffracer-backdoor/tests/test_one_switch.py` is not: the removed
names never reached `start()`, so behaviour is unchanged and only the names
moved. A sweep is the honest instrument for a removal.

`is_backdoor_enabled` is swept too, and it is the reason a name sweep is worth
having at all here rather than a smaller assertion about the module. It escaped
`test_no_orphan_defs.py` for a stated reason rather than a broken one: that
sweep counts references repo-wide by name, and the package `__init__` that
exports a name imports it, so an exported helper always has one reference and
never reaches the orphan list. Its own docstring says this about exported
exception types. The same property covered this.

A future document that needs to name one of these -- an upgrade note for a
release that removes them -- belongs in a migration guide, and adding it means
adding an exemption here on purpose rather than discovering the guard is quiet.
There are deliberately no exemptions today.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _require_git():
    if not (REPO / ".git").is_dir():
        pytest.skip("Not running inside a git repository (release tarball)")


REMOVED = {
    "CLIFFRACER_DISABLE_BACKDOOR": "a second spelling of the switch, read by nothing",
    "CLIFFRACER_ENV": "a deployment-environment fact the application already owns",
    "is_backdoor_enabled": "the helper that read both, called by nothing",
    "use_ipython": "a setting that was stored and never read",
    "ipython": "the prompt is aioconsole; the IPython path was never called",
}
# Case-insensitive so one entry covers `IPython`, `ipython` and `IPYTHON`.
# `use_ipython` needs its own entry regardless: an underscore is a word
# character, so `\bipython\b` does not match inside it.
PATTERN = re.compile("|".join(rf"\b{re.escape(name)}\b" for name in REMOVED), re.I)

# TWO EXEMPTIONS, each for a file whose job requires the name.
# A test that pins an absence must name the absent identifier.
# Exactly one test path is exempt, not its directory, so new files cannot
# unintentionally inherit the exemption.
#
# CHANGELOG.md records past releases and is exempt.
EXEMPT = {
    "packages/cliffracer-backdoor/tests/test_one_switch.py",
    }


def swept_files() -> list[Path]:
    """Tracked Python under src/ and packages/, plus every tracked Markdown.

    From `git ls-files` rather than a glob: a glob walks whatever is on disk,
    so an untracked scratch copy fails the build for one contributor and a file
    deleted from the index but left behind keeps being checked.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "src/*.py", "packages/*.py", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO / rel for rel in out if rel not in EXEMPT]


def mentions(paths=None) -> list[str]:
    found = []
    for path in paths if paths is not None else swept_files():
        rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if PATTERN.search(line):
                found.append(f"{rel}:{number} {line.strip()[:100]}")
    return found


@pytest.mark.unit
def test_the_sweep_reads_the_tree():
    """ "0 mentions" is what a clean tree and a broken file list look like alike."""
    # Verify the sweep matches a minimum expected number of files across the repo.
    files = swept_files()
    assert len(files) >= 60, f"only found {len(files)} files to sweep"
    assert any(f.suffix == ".md" for f in files), "no Markdown in the sweep"
    assert any(f.suffix == ".py" for f in files), "no Python in the sweep"
    assert all(f.exists() for f in files), "git listed a file that is not on disk"


@pytest.mark.unit
def test_no_code_or_document_names_a_removed_switch():
    found = mentions()
    assert not found, (
        "`BackdoorConfig.enabled` / `CLIFFRACER_BACKDOOR_ENABLED` is the only "
        "switch. These name one of "
        + ", ".join(f"{k} ({v})" for k, v in REMOVED.items())
        + ":\n  "
        + "\n  ".join(found)
    )


@pytest.mark.unit
def test_the_exemptions_still_earn_themselves():
    """Verify each exempt file still contains the terms it was exempted for."""
    for rel in EXEMPT:
        path = REPO / rel
        assert path.exists(), f"exempt file that does not exist: {rel}"
        assert PATTERN.search(path.read_text()), (
            f"{rel} no longer names any removed switch, so its exemption is "
            "doing nothing -- delete it"
        )


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(REMOVED))
def test_CONTROL_the_sweep_catches_each_name(tmp_path: Path, name: str):
    """A sweep that matches nothing passes a tree that still has all three."""
    doc = tmp_path / "d.md"
    doc.write_text(f"Set `{name}` to turn the console off.\n")
    assert mentions([doc]), f"not caught: {name}"


@pytest.mark.unit
def test_CONTROL_the_switch_that_stays_is_not_swept(tmp_path: Path):
    """The other half: a sweep that flags the surviving name would force the
    documentation to stop naming the one switch there is."""
    doc = tmp_path / "d.md"
    doc.write_text("Set `CLIFFRACER_BACKDOOR_ENABLED=true` to start the console.\n")
    assert not mentions([doc]), "the sweep flagged the switch that stays"

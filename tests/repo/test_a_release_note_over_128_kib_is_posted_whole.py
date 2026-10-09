"""A release note larger than one argument may be is posted whole, on both platforms.

Linux holds one environment string or argument to 128 KiB (`MAX_ARG_STRLEN`), and `exec` fails
with "Argument list too long" past that. A release's note, the changelog section plus every
commit, runs past it, so the release-note steps keep the note in files: the Gitea step builds its
JSON body from the note's file and posts it with `curl -d @file`, and the mirror's step hands it to
`gh release create --notes-file`.

Each step's own `run:` script is run here, as CI runs it, with three stand-ins: a renderer that
writes a note over 128 KiB, and `curl` and `gh` that keep what they were given. The note that
arrives must be the note that was rendered.
"""

import errno
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
TAG = "v9.9.9"

#: One environment string or argument may hold this many bytes, terminator included.
MAX_ARG_STRLEN = 128 * 1024

#: Characters JSON has to escape, and text that is not ASCII, so the body is built, not pasted.
LINE = '- a "quoted" \\ back-slashed, tabbed\tcommit — ünïcode {braces} $NOT_A_VARIABLE\n'


def note() -> str:
    lines = ["## Breaking changes\n", "\n", "- one\n", "\n", "## Commits\n", "\n"]
    while sum(len(line.encode()) for line in lines) <= 2 * MAX_ARG_STRLEN:
        lines.append(LINE)
    return "".join(lines)


RENDERER = """\
import sys
from pathlib import Path

if "--count-breaking" in sys.argv:
    sys.stdin.read()
    print(1)
else:
    sys.stdout.write(Path(__file__).with_name("note.txt").read_text(encoding="utf-8"))
"""

#: Keeps the file `-d @FILE` names, and answers as Gitea does when it creates a release.
CURL = """\
#!/bin/bash
while [ $# -gt 0 ]; do
  case "$1" in
    -d) cp "${2#@}" "$CAPTURE/body.json"; shift ;;
    -o) : > "$2"; shift ;;
  esac
  shift
done
printf 201
"""

#: Keeps the file `--notes-file` names, or the text `--notes` was given.
GH = """\
#!/bin/bash
while [ $# -gt 0 ]; do
  case "$1" in
    --notes-file) cp "$2" "$CAPTURE/notes.txt"; shift ;;
    --notes) printf '%s' "$2" > "$CAPTURE/notes.txt"; shift ;;
  esac
  shift
done
"""


def step(workflow: str, step_id: str) -> str:
    """The `run:` script of the release job's step `step_id`, its expressions filled in."""
    jobs = yaml.safe_load((REPO / workflow).read_text())["jobs"]
    run = next(s for s in jobs["release"]["steps"] if s.get("id") == step_id)["run"]
    for expression, value in {
        "${{ steps.decide.outputs.tag }}": TAG,
        "${{ github.server_url }}": "http://forge.example.invalid",
        "${{ github.repository }}": "warehouse/orders",
    }.items():
        run = run.replace(expression, value)
    assert "${{" not in run, f"an expression this test does not fill in: {run}"
    return run


@pytest.fixture
def stage(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A working directory with the stand-in renderer, and an environment whose PATH finds the
    stand-in `curl` and `gh` first."""
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    (work / "scripts" / "release_note.py").write_text(RENDERER)
    (work / "scripts" / "note.txt").write_text(note(), encoding="utf-8")
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, text in (("curl", CURL), ("gh", GH)):
        (tools / name).write_text(text)
        (tools / name).chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir()
    env = dict(
        os.environ,
        PATH=f"{tools}{os.pathsep}{os.environ['PATH']}",
        CAPTURE=str(capture),
        RELEASE_TOKEN="token",
        GH_TOKEN="token",
    )
    return work, env


def run(script: str, work: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], cwd=work, env=env, capture_output=True, text=True, timeout=60
    )


def test_CONTROL_the_note_does_not_fit_in_one_environment_string():
    """Without this, the tests below could pass on a note small enough to have worked before."""
    text = note()
    assert len(text.encode()) > MAX_ARG_STRLEN

    with pytest.raises(OSError) as refused:
        subprocess.run(["true"], env={"NOTES": text}, check=False)

    assert refused.value.errno == errno.E2BIG


@pytest.mark.gitea_checkout
def test_the_gitea_step_posts_a_json_body_holding_the_whole_note(stage):
    work, env = stage

    result = run(step(".gitea/workflows/ci.yml", "relnote"), work, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Argument list too long" not in result.stderr
    body = json.loads((Path(env["CAPTURE"]) / "body.json").read_text(encoding="utf-8"))
    assert body == {"tag_name": TAG, "name": TAG, "body": note().rstrip("\n")}
    assert f"Release note created for {TAG}" in result.stdout


def test_the_mirror_step_hands_gh_the_whole_note(stage):
    work, env = stage

    result = run(step(".github/workflows/ci.yml", "relnote"), work, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Argument list too long" not in result.stderr
    notes = Path(env["CAPTURE"]) / "notes.txt"
    assert notes.exists(), result.stderr
    assert notes.read_text(encoding="utf-8") == note()

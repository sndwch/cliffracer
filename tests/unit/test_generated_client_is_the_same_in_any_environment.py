"""The generated client is the same bytes wherever and whenever it is generated.

That is what makes it committable: regenerating it on a laptop, in CI or on a staging host must
not produce a diff. The emitter says so ("deterministic across environments without timestamps or
system metadata"), and two kinds of thing could break it:

- a value read from the machine or the clock written into the file (a build time, a hostname, a
  user, a working directory, a pid, a uuid), and
- an order that depends on the process (iterating a set of strings under a different hash seed).

The first is read in this process, with each source of machine state replaced by a value no real
output contains and the whole emitted text searched for them; the clock cannot be replaced for
code that reads it directly, so the text is searched for the present year, today's date and an
epoch-seconds number instead. The second is read by generating twice in subprocesses that differ in
hash seed, working directory, home, user, locale and time zone, and comparing the bytes.
"""

import datetime
import getpass
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

import cliffracer
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import Description

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

#: The `src` directory this process imported `cliffracer` from. A child is pointed at it, so it runs
#: the code under test: `REPO / "src"` is the tree this file sits in, which is another tree's code
#: when one tree's tests are run against another tree's `src`.
IMPORTED_SRC = str(Path(cliffracer.__file__).resolve().parents[1])
FIXTURE_DESCRIPTION = REPO / "tests" / "fixtures" / "typed_client" / "description.json"

SENTINELS = {
    "hostname": "sentinel-host-7f3a91",
    "user": "sentinel-user-7f3a91",
    "working directory": "/sentinel/cwd/7f3a91",
    "home": "/sentinel/home/7f3a91",
    "temp directory": "/sentinel/tmp/7f3a91",
    "pid": "424242",
    "uuid": "7f3a9100-0000-4000-8000-000000000001",
}

ANY_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
ANY_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
ANY_EPOCH_SECONDS = re.compile(r"\b1[0-9]{9}\b")


def _description() -> Description:
    return Description.from_dict(json.loads(FIXTURE_DESCRIPTION.read_text()))


def _leaks(text: str) -> list[str]:
    """What of the machine and the clock `text` carries."""
    found = [name for name, value in SENTINELS.items() if value in text]
    now = datetime.datetime.now(datetime.UTC)
    here = datetime.datetime.now()
    for label, pattern in (
        ("a year", ANY_YEAR),
        ("a timestamp", ANY_TIMESTAMP),
        ("an epoch-seconds number", ANY_EPOCH_SECONDS),
    ):
        if pattern.search(text):
            found.append(label)
    # An epoch number is reported above, and its digits can hold the year; any other run of digits
    # that holds a date, a compact stamp among them, is still today's date.
    without_epochs = ANY_EPOCH_SECONDS.sub(" ", text)
    for date in {now.strftime("%Y-%m-%d"), here.strftime("%Y-%m-%d"), str(now.year)}:
        if date in without_epochs:
            found.append(f"today's date ({date})")
    return found


@pytest.fixture
def machine_state_replaced(monkeypatch):
    """Every source of machine state the emitter could read, answering with a sentinel."""
    monkeypatch.setattr(socket, "gethostname", lambda: SENTINELS["hostname"])
    monkeypatch.setattr(platform, "node", lambda: SENTINELS["hostname"])
    monkeypatch.setattr(getpass, "getuser", lambda: SENTINELS["user"])
    monkeypatch.setattr(os, "getcwd", lambda: SENTINELS["working directory"])
    monkeypatch.setattr(os, "getpid", lambda: int(SENTINELS["pid"]))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: SENTINELS["temp directory"])
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(SENTINELS["uuid"]))
    for name in ("USER", "LOGNAME", "USERNAME"):
        monkeypatch.setenv(name, SENTINELS["user"])
    for name in ("HOSTNAME", "COMPUTERNAME"):
        monkeypatch.setenv(name, SENTINELS["hostname"])
    monkeypatch.setenv("HOME", SENTINELS["home"])
    monkeypatch.setenv("PWD", SENTINELS["working directory"])


def test_the_emitted_text_carries_nothing_from_the_machine_or_the_clock(machine_state_replaced):
    text = emit(_description())

    assert _leaks(text) == [], "the generated client carries machine or clock state"


def test_CONTROL_every_source_replaced_above_would_be_found_in_the_text(machine_state_replaced):
    """The test above passes for any emitter if the replacements never reach what it reads.

    Each replaced source is read the way a leaking emitter would read it, and the text it would
    write is checked: the sentinel must come through and `_leaks` must name it.
    """
    read = {
        "hostname": socket.gethostname(),
        "user": getpass.getuser(),
        "working directory": os.getcwd(),
        "home": os.environ["HOME"],
        "temp directory": tempfile.gettempdir(),
        "pid": str(os.getpid()),
        "uuid": str(uuid.uuid4()),
    }

    assert read == SENTINELS
    for name, value in read.items():
        assert name in _leaks(f"generated_by: {value}"), name
    assert _leaks(f"generated_at: {datetime.datetime.now(datetime.UTC).isoformat()}") != []
    assert _leaks(f"built: {int(time.time())}") == ["an epoch-seconds number"]


def test_an_epoch_whose_digits_hold_the_year_is_only_an_epoch():
    """An epoch second whose digits contain the current year is named once, as an epoch."""
    year = str(datetime.datetime.now(datetime.UTC).year)
    epoch = f"1{year}00000"
    assert ANY_EPOCH_SECONDS.fullmatch(epoch), epoch

    assert _leaks(f"built: {epoch}") == ["an epoch-seconds number"]


@pytest.mark.parametrize("pattern", ["%Y%m%d", "%Y%m%d%H%M%S"], ids=["date", "date-and-time"])
def test_a_compact_stamp_is_still_todays_date(pattern):
    """A stamp with no separators is a run of digits that starts with the year: only an epoch number
    is set aside before the dates are searched for, so the stamp is still found."""
    now = datetime.datetime.now(datetime.UTC)
    year = str(now.year)

    assert f"today's date ({year})" in _leaks(f"stamp: {now.strftime(pattern)}")


def test_CONTROL_a_year_standing_alone_is_still_a_date():
    year = str(datetime.datetime.now(datetime.UTC).year)

    assert f"today's date ({year})" in _leaks(f"generated in {year}")
    assert f"today's date ({year})" in _leaks(f"generated in {year}.")


# The imported `src` first, so the subprocess imports the code under test whatever the interpreter's
# own install points at; the repo root is what makes `tests.fixtures...` importable.
SUBPROCESS_PYTHONPATH = os.pathsep.join([IMPORTED_SRC, str(REPO)])


def _generate(tmp_path: Path, name: str, **env: str) -> bytes:
    home = tmp_path / name / "home"
    work = tmp_path / name / "cwd"
    home.mkdir(parents=True)
    work.mkdir(parents=True)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "cliffracer.generate_client.cli",
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "warehouse_e2e",
            "--version",
            "3.1.4",
        ],
        capture_output=True,
        cwd=work,
        timeout=120,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": SUBPROCESS_PYTHONPATH,
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(home),
            **env,
        },
    )
    assert done.returncode == 0, done.stderr.decode(errors="replace")[-1500:]
    assert done.stdout.startswith(b'"""Generated by cliffracer-generate-client'), done.stdout[:200]
    return done.stdout


def test_two_environments_generate_the_same_bytes(tmp_path):
    first = _generate(
        tmp_path,
        "first",
        PYTHONHASHSEED="1",
        USER="alice",
        LOGNAME="alice",
        TZ="UTC",
        LC_ALL="C.UTF-8",
    )
    second = _generate(
        tmp_path,
        "second",
        PYTHONHASHSEED="2",
        USER="bob",
        LOGNAME="bob",
        TZ="Pacific/Auckland",
        LC_ALL="C",
    )

    assert first == second
    assert len(first) > 1000, "the comparison ran over a client, not over an empty file"


def test_CONTROL_the_two_environments_do_differ_where_the_process_can_see_it(tmp_path):
    """Equal bytes mean something only if the two runs really ran under different conditions.

    The hash seed is the one the interpreter exposes directly: the same string hashes
    differently under the two seeds the test above uses.
    """
    probe = "import sys; print(hash('cliffracer'))"
    seen = {
        seed: subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("1", "2")
    }

    assert seen["1"] != seen["2"], seen


def test_CONTROL_the_subprocess_imports_the_tree_under_test():
    """The byte comparison reads the emitter under test only if the subprocess imports it."""
    done = subprocess.run(
        [sys.executable, "-c", "import cliffracer, sys; sys.stdout.write(cliffracer.__file__)"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": SUBPROCESS_PYTHONPATH},
    )

    assert Path(done.stdout).resolve().parents[1] == Path(IMPORTED_SRC), (done.stdout, IMPORTED_SRC)

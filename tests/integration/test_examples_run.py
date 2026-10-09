"""Verify runnable examples start and execute cleanly against a NATS broker."""

import ast
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import broker_url, configured_broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"
# Subprocess bootstrap setting sys.path and propagating broker configuration.
_BOOTSTRAP = f"""
import os
import runpy
import sys

sys.path.insert(0, {str(REPO)!r})
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[2])))
from cliffracer import ServiceConfig

# Every example inherits the default health port, and a port already in use
# fails loudly, so two examples running at once would contend for 8000. Port 0
# is the explicit way to ask the operating system for a free one; each example
# process then gets its own and reports it on /info.
ServiceConfig.model_fields["health_port"].default = 0
url = sys.argv[1]
if url:
    ServiceConfig.model_fields["nats_url"].default = url
ServiceConfig.model_rebuild(force=True)
# The example sees the argv it documents (`python example.py`): the broker URL and the path above
# are this bootstrap's own arguments, and an example that dispatches on `len(sys.argv)` would
# otherwise take its usage branch and exit 0 without building a service.
example = sys.argv[2]
sys.argv = [example]
runpy.run_path(example, run_name="__main__")
"""

# Map of example relative paths to skip reason.
SKIP: dict[str, str] = {}

STARTUP_SECONDS = 6
SHUTDOWN_SECONDS = 6

#: What a long-running example prints once the thing it demonstrates has happened once: its first
#: order created, its first timer run, its first RPC answered. Never at "started".
READY_MARKER = "EXAMPLE READY:"
#: How long a long-running example has to print `READY_MARKER`. Its slowest step, measured on a
#: quiet host, came 5.4 s after it started; one that has not happened by this is a failed example,
#: not a slow one.
READY_SECONDS = 30
#: How long an example keeps running after its marker before it is stopped.
SETTLE_SECONDS = 1


# The orchestrator catches service exceptions, logs them, and keeps the process
# alive to restart them (runners/orchestrator.py:129). A process might still be
# running when we send SIGINT even if all its services have crashed.
#
# We must scan the output for crash markers to ensure we catch constructor or
# runtime crashes inside orchestrated examples.
_CRASH_MARKER = "Service crashed:"
_CRASH_EXCERPT = 160


def crashed_services(output: str) -> list[str]:
    """One short line per distinct crash, in first-seen order, with a count.

    TRIMMED AND DEDUPED, and both halves are the difference between a usable
    failure and an unusable one. An example that logs JSON puts the whole
    traceback inside a single physical line -- the raw ecommerce crash line is
    ~4 KB -- and the orchestrator RESTARTS a crashed service, so the same crash
    arrives once per attempt. Reporting them raw gave a multi-megabyte
    assertion message that nobody would read to the end, which is a way of not
    reporting at all.
    """
    seen: dict[str, int] = {}
    for line in output.splitlines():
        i = line.find(_CRASH_MARKER)
        if i < 0:
            continue
        excerpt = line[i : i + _CRASH_EXCERPT]
        # A JSON-logging example escapes its newlines, so the traceback that
        # follows is on this same physical line: cut at whichever comes first.
        for stop in ("\\n", "\n"):
            if stop in excerpt:
                excerpt = excerpt.split(stop, 1)[0]
        excerpt = excerpt.strip()
        seen[excerpt] = seen.get(excerpt, 0) + 1
    return [f"{text}   [x{n}]" if n > 1 else text for text, n in seen.items()]


def is_main_block(node: ast.AST) -> bool:
    """`if __name__ == "__main__":` at module level, however it is spelled.

    Matched structurally rather than by source text, so `"__main__" == __name__`
    and a chained comparison are the same thing to it.
    """
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or not test.ops or not isinstance(test.ops[0], ast.Eq):
        return False
    sides = [test.left, test.comparators[0]]
    names = {n.id for n in sides if isinstance(n, ast.Name)}
    consts = {n.value for n in sides if isinstance(n, ast.Constant)}
    return "__name__" in names and "__main__" in consts


def runnable_reason(tree: ast.Module) -> str | None:
    """Return reason why an example script is runnable, or None if skipped."""
    if any(
        isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == "main"
        for n in tree.body
    ):
        return "defines main()"
    if any(isinstance(n, ast.ClassDef) for n in tree.body):
        return "defines a class"
    if any(is_main_block(n) for n in tree.body):
        return "has an __main__ block"
    return None


def _runnable() -> list[Path]:
    """Examples that do something: a main(), a class, or a `__main__` block."""
    out = []
    for path in sorted(EXAMPLES.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            out.append(path)  # a syntax error must fail, not be skipped
            continue
        if runnable_reason(tree) is not None:
            out.append(path)
    return out


RUNNABLE = _runnable()


def test_CONTROL_the_sweep_found_examples():
    """A sweep that walks nothing passes every case below."""
    assert len(RUNNABLE) > 10, f"only {len(RUNNABLE)} runnable examples found"


def _spawn_example(path: Path, cwd: Path) -> subprocess.Popen:
    """Start one example the way test_the_example_starts does."""
    asked_for = configured_broker_url() or ""
    # `CLIFFRACER_EXAMPLE_PORTS=auto` makes an example ask the OS for its ports
    # instead of binding the fixed ones its own URLs document. Without it a
    # second copy -- another tier on this host, or the pair below -- cannot bind
    # and never starts.
    env = dict(
        os.environ,
        PYTHONUNBUFFERED="1",
        NATS_URL=broker_url(),
        CLIFFRACER_EXAMPLE_PORTS="auto",
    )
    return subprocess.Popen(
        [sys.executable, "-c", _BOOTSTRAP, asked_for, str(path)],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def examples_naming_an_explicit_health_port() -> set[Path]:
    """Examples that pass `health_port=` themselves.

    Derived rather than listed: the runner's bootstrap only moves examples that
    INHERIT the default port, so an example choosing its own is outside what
    the bootstrap can do. A new one must show up here rather than be exempted
    by a name someone remembered to add.
    """
    found: set[Path] = set()
    for path in RUNNABLE:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "health_port":
                found.add(path)
                break
    return found


_HEALTH_BOUND = "health listener on http://"
_HEALTH_BIND_FAILED = "health listener failed to bind"


class _Output:
    """An example process's output, collected as it arrives so a test can wait on it."""

    def __init__(self, proc: subprocess.Popen, listeners: int) -> None:
        self.proc = proc
        self.listeners = listeners
        self._lines: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.append(line)

    @property
    def text(self) -> str:
        return "".join(self._lines)

    def settled(self) -> bool:
        """Whether each of its health listeners has bound or failed to, or the process ended."""
        # Lines, not occurrences: an example that logs JSON carries the message twice in one line.
        reported = sum(
            _HEALTH_BOUND in line or _HEALTH_BIND_FAILED in line for line in self.text.splitlines()
        )
        return reported >= self.listeners or self.proc.poll() is not None

    def marked(self) -> bool:
        """Whether the example has printed `READY_MARKER`."""
        return any(line.startswith(READY_MARKER) for line in self._lines)

    def join(self) -> None:
        """Wait for the reader to reach the end of the output, once the process has ended."""
        self._reader.join()

    def stop(self) -> str:
        """SIGINT the example, SIGKILL it after `SHUTDOWN_SECONDS`, and return all it wrote."""
        os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
        try:
            self.proc.wait(timeout=SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            self.proc.wait()
        self._reader.join()
        return self.text


def _start_two_copies(example: Path, tmp_path: Path, listeners: int) -> tuple[str, str]:
    """Run two copies of `example` until each of their `listeners` health listeners has bound or
    failed to, then stop both.

    They are given up to `STARTUP_SECONDS` to get there. A count that is too high only waits the
    whole of it.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    copies = [_Output(_spawn_example(example, tmp_path / d), listeners) for d in ("a", "b")]
    deadline = time.monotonic() + STARTUP_SECONDS
    while not all(c.settled() for c in copies) and time.monotonic() < deadline:
        time.sleep(0.05)
    first, second = (c.stop() for c in copies)
    return first, second


def test_two_copies_of_one_example_do_not_contend_for_a_health_port(tmp_path: Path):
    """Two example processes at once must each get their own health port.

    The same example twice, rather than two different ones: most examples
    finish their script without ever binding, so a pair chosen by position can
    pass while only one of them ever started a listener. Asserting that BOTH
    logged a bound listener is what stops this passing vacuously -- without it
    the test is green with the port-0 default removed, which is how the first
    version of it was wrong.

    Scoped to the health listener: a listener that cannot bind fails the
    service at start, so an example that pins a port reds this too.
    """
    explicit = examples_naming_an_explicit_health_port()
    inheriting = [
        p for p in RUNNABLE if p not in explicit and str(p.relative_to(EXAMPLES)) not in SKIP
    ]
    assert inheriting, "no example inherits the default health port"
    example = EXAMPLES / "timer" / "timer_service_example.py"
    assert example in inheriting, "this example must still inherit the default port"

    out_first, out_second = _start_two_copies(example, tmp_path, listeners=1)

    for label, out in (("first", out_first), ("second", out_second)):
        assert _HEALTH_BOUND in out, (
            f"the {label} copy never bound a health listener, so this test "
            f"exercised no contention:\n{out[-1500:]}"
        )
        assert _HEALTH_BIND_FAILED not in out, (
            f"the {label} copy could not bind its health port:\n{out[-1500:]}"
        )

    ports = {
        line.split(_HEALTH_BOUND)[1].split("/")[0]
        for out in (out_first, out_second)
        for line in out.splitlines()
        if _HEALTH_BOUND in line
    }
    assert len(ports) >= 2, f"both copies bound the same address: {sorted(ports)}"


def test_two_copies_of_a_port_naming_example_do_not_contend(tmp_path: Path):
    """The same check, on an example the test above EXCLUDES.

    `test_two_copies_of_one_example_do_not_contend_for_a_health_port` computes
    `examples_naming_an_explicit_health_port()` and removes those from the pool
    it draws from, so its sample is by construction the examples that cannot
    contend -- the bootstrap has already moved their ports. It was green for as
    long as the examples it excluded were the broken ones.

    This runs the excluded case. It passes because those examples now ask for
    their ports through `_port()` rather than binding the numbers their URLs
    document; against examples that named the numbers outright it fails, which
    is the reading that says this test is about something.
    """
    example = EXAMPLES / "basic" / "async_patterns.py"
    assert example in examples_naming_an_explicit_health_port(), (
        f"{example.name} no longer names its own health port, so this test has "
        "stopped covering the case it exists for; pick another that does"
    )

    out_first, out_second = _start_two_copies(example, tmp_path, listeners=3)

    for label, out in (("first", out_first), ("second", out_second)):
        assert _HEALTH_BOUND in out, (
            f"the {label} copy never bound a health listener, so this exercised "
            f"no contention:\n{out[-1500:]}"
        )
        assert _HEALTH_BIND_FAILED not in out, (
            f"the {label} copy could not bind its health port:\n{out[-1500:]}"
        )
    ports = {
        line.split(_HEALTH_BOUND)[1].split("/")[0]
        for out in (out_first, out_second)
        for line in out.splitlines()
        if _HEALTH_BOUND in line
    }
    assert len(ports) >= 2, f"both copies bound the same address: {sorted(ports)}"


@pytest.mark.parametrize("path", RUNNABLE, ids=lambda p: str(p.relative_to(EXAMPLES)))
def test_the_example_starts(path: Path, tmp_path: Path):
    """Each example does what it is for, and a long-running one says when it has.

    A demo that scripts itself to completion passes by exiting 0. A long-running example passes by
    printing `READY_MARKER` within `READY_SECONDS` and then stopping cleanly at SIGINT: the marker
    is printed after the thing the example demonstrates has happened once (an order created, a
    timer run, an RPC answered), so the test fails, by name, on an example whose step never happens,
    however healthy it otherwise looks. A crash line or an exit ends the wait at once.

    What it does not catch: a crash more than `SETTLE_SECONDS` after the marker, or a fault in a
    step the example reaches only after it, such as a timer whose first firing comes later.
    """
    rel = str(path.relative_to(EXAMPLES))
    if rel in SKIP:
        pytest.skip(SKIP[rel])

    proc = _spawn_example(path, tmp_path)
    output = _Output(proc, listeners=0)

    def running_without_a_crash() -> bool:
        return proc.poll() is None and not crashed_services(output.text)

    deadline = time.monotonic() + READY_SECONDS
    while running_without_a_crash() and not output.marked() and time.monotonic() < deadline:
        time.sleep(0.05)
    settled = time.monotonic() + SETTLE_SECONDS
    while output.marked() and running_without_a_crash() and time.monotonic() < settled:
        time.sleep(0.05)

    if proc.poll() is None:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        try:
            proc.wait(timeout=SHUTDOWN_SECONDS)
            how = "SIGINT"
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            output.join()
            pytest.fail(f"{rel} ignored SIGINT and had to be killed\n{output.text[-2000:]}")
    else:
        how = "exited"
    output.join()
    out, rc = output.text, proc.returncode

    # Check for service crashes in captured output.
    crashed = crashed_services(out)
    assert not crashed, (
        f"{rel} stayed up but {len(crashed)} service(s) crashed:\n  "
        + "\n  ".join(crashed[:5])
        + f"\n\n{out[-2000:]}"
    )

    assert how == "exited" or output.marked(), (
        f"{rel} was still running after {READY_SECONDS} s and never printed {READY_MARKER!r}: "
        f"what it demonstrates did not happen\n{out[-2000:]}"
    )

    # A long-running service is SUPPOSED to still be up at SIGINT; a demo that
    # finishes its script is supposed to exit 0. Both pass; anything else is
    # the example failing, and the output says how.
    ok = (how == "SIGINT" and rc in (0, -signal.SIGINT, 130)) or (how == "exited" and rc == 0)
    assert ok, f"{rel} {how} rc={rc}\n{out[-2000:]}"

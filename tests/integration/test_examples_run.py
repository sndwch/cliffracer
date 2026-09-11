"""Verify runnable examples start and execute cleanly against a NATS broker."""

import ast
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import broker_url, configured_broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"
# Subprocess bootstrap setting sys.path and propagating broker configuration.
_BOOTSTRAP = """
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[2])))
url = sys.argv[1]
if url:
    from cliffracer import ServiceConfig

    ServiceConfig.model_fields["nats_url"].default = url
    ServiceConfig.model_rebuild(force=True)
runpy.run_path(sys.argv[2], run_name="__main__")
"""

# Map of example relative paths to skip reason.
SKIP: dict[str, str] = {}

STARTUP_SECONDS = 6
SHUTDOWN_SECONDS = 6


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


@pytest.mark.parametrize("path", RUNNABLE, ids=lambda p: str(p.relative_to(EXAMPLES)))
def test_the_example_starts(path: Path, tmp_path: Path):
    rel = str(path.relative_to(EXAMPLES))
    if rel in SKIP:
        pytest.skip(SKIP[rel])

    # Execute in temporary directory to isolate file output.
    asked_for = configured_broker_url() or ""
    env = dict(os.environ, PYTHONUNBUFFERED="1", NATS_URL=broker_url())
    proc = subprocess.Popen(
        [sys.executable, "-c", _BOOTSTRAP, asked_for, str(path)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=STARTUP_SECONDS)
        rc, how = proc.returncode, "exited"
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=SHUTDOWN_SECONDS)
            rc, how = proc.returncode, "SIGINT"
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            out, _ = proc.communicate()
            pytest.fail(f"{rel} ignored SIGINT and had to be killed\n{out[-2000:]}")

    # Check for service crashes in captured output.
    crashed = crashed_services(out)
    assert not crashed, (
        f"{rel} stayed up but {len(crashed)} service(s) crashed:\n  "
        + "\n  ".join(crashed[:5])
        + f"\n\n{out[-2000:]}"
    )

    # A long-running service is SUPPOSED to still be up at SIGINT; a demo that
    # finishes its script is supposed to exit 0. Both pass; anything else is
    # the example failing, and the output says how.
    ok = (how == "SIGINT" and rc in (0, -signal.SIGINT, 130)) or (how == "exited" and rc == 0)
    assert ok, f"{rel} {how} rc={rc}\n{out[-2000:]}"

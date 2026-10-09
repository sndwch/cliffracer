"""The runner block records the job's memory cap, and a different cap does not stop a run being scored.

`total_ram_gb` is the host's memory, which does not move when the runner's job container is given
more or less. The control group's `memory.max` is the figure that does. It is recorded beside the
load, as a condition of the run: `null` where there is no limit or no file, and not in
`RUNNER_SPEC_FIELDS`, so a cap that differs from the baseline's is on the record and does not turn
the comparison into a refusal. Comparing it would refuse every run until the baseline is recorded
again on the benchmark host.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_benchmark_regression.py"
BASELINE = REPO / "benchmark_baseline.json"

sys.path.insert(0, str(REPO / "scripts"))
from check_benchmark_regression import EXIT_NOT_SCORED, runner_spec  # noqa: E402

GIB = 1024**3


@pytest.fixture(autouse=True)
def _no_monitor_read(monkeypatch):
    """`get_environment_context` reads the broker's monitoring port when it can. These tests are
    about the memory limit, so that read is refused here rather than answered by whatever happens to
    be listening on the host."""
    import urllib.error
    import urllib.request

    def refused(*args, **kwargs):
        raise urllib.error.URLError("the monitoring port is not read by this test")

    monkeypatch.setattr(urllib.request, "urlopen", refused)


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "memory.max"
    path.write_text(text)
    return path


def test_a_number_is_the_limit_in_gib(tmp_path: Path):
    from tests.benchmark.benchmarks import cgroup_memory_limit_gb

    assert cgroup_memory_limit_gb(_file(tmp_path, f"{8 * GIB}\n")) == 8.0
    assert cgroup_memory_limit_gb(_file(tmp_path, f"{GIB // 2}\n")) == 0.5
    assert cgroup_memory_limit_gb(_file(tmp_path, str(4 * GIB))) == 4.0


def test_max_is_no_limit(tmp_path: Path):
    from tests.benchmark.benchmarks import cgroup_memory_limit_gb

    assert cgroup_memory_limit_gb(_file(tmp_path, "max\n")) is None


def test_an_absent_file_is_no_figure(tmp_path: Path):
    from tests.benchmark.benchmarks import cgroup_memory_limit_gb

    assert cgroup_memory_limit_gb(tmp_path / "does-not-exist") is None


@pytest.mark.parametrize("text", ["", "\n", "unlimited", "-1", "8 GiB", "1.5e9"])
def test_a_value_that_is_not_a_byte_count_is_no_figure(tmp_path: Path, text: str):
    from tests.benchmark.benchmarks import cgroup_memory_limit_gb

    assert cgroup_memory_limit_gb(_file(tmp_path, text)) is None


def test_a_directory_where_the_file_should_be_is_no_figure(tmp_path: Path):
    from tests.benchmark.benchmarks import cgroup_memory_limit_gb

    assert cgroup_memory_limit_gb(tmp_path) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [(str(8 * GIB), 8.0), ("max", None), (None, None)],
    ids=["cap", "max", "absent"],
)
def test_the_recorded_block_carries_what_the_host_reports(monkeypatch, tmp_path, text, expected):
    """The code that writes the block, driven through the path it reads, not a file that already
    holds a figure."""
    from tests.benchmark import benchmarks

    path = tmp_path / "memory.max"
    if text is not None:
        path.write_text(text)
    monkeypatch.setattr(benchmarks, "CGROUP_MEMORY_MAX", path)

    runner = benchmarks.get_environment_context()["runner"]

    assert "memory_limit_gb" in runner, "the key is present, so absence reads as 'not measured'"
    assert runner["memory_limit_gb"] == expected


def test_the_limit_is_not_a_spec_field():
    block = {"cpu_count": 20, "total_ram_gb": 125.47, "memory_limit_gb": 8.0}

    assert "memory_limit_gb" not in runner_spec(block)


def _run(current: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--baseline",
            str(BASELINE),
            "--current",
            str(current),
            "--threshold",
            "0.15",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def _run_with_limit(tmp_path: Path, limit: float | None, *, regress: bool) -> Path:
    data = json.loads(BASELINE.read_text())
    if regress:
        data["metrics"]["rpc"]["concurrency_10"]["throughput_msgs_sec"] *= 0.5
    runner = data["environment"]["runner"]
    runner.pop("load_average", None)
    runner.pop("runner_name", None)
    runner["memory_limit_gb"] = limit
    out = tmp_path / "current.json"
    out.write_text(json.dumps(data))
    return out


def test_a_different_cap_is_scored_not_refused_as_another_machine(tmp_path: Path):
    """A halved metric on a run whose cap differs from the baseline's is a regression (exit 1),
    not 'not scored' (exit 2): the cap alone does not make it a different machine."""
    result = _run(_run_with_limit(tmp_path, 4.0, regress=True))

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.returncode != EXIT_NOT_SCORED


def test_CONTROL_a_run_that_differs_in_hardware_is_still_refused(tmp_path: Path):
    path = _run_with_limit(tmp_path, 4.0, regress=True)
    data = json.loads(path.read_text())
    data["environment"]["runner"]["cpu_count"] = 999
    path.write_text(json.dumps(data))

    assert _run(path).returncode == EXIT_NOT_SCORED


def test_CONTROL_a_run_with_the_baselines_numbers_and_another_cap_passes(tmp_path: Path):
    result = _run(_run_with_limit(tmp_path, None, regress=False))

    assert result.returncode == 0, result.stdout + result.stderr

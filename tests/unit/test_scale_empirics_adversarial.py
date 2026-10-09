"""Adversarial stress testing and empirical verification suite for framework hardening and scale empirics.

Covers:
1. Token Revocation & JTI Invalidation
2. Extension Argument Cloning & Isolation
3. Collocation Safety & Health Probes
4. AST Complexity Linter & Logging Controls
5. Continuous Benchmark Regression Checker
"""

from __future__ import annotations

import ast
import errno
import json
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cliffracer_auth import AuthConfig, SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import (
    Extension,
    ExtensionIsolationError,
    SharedDependency,
    _safe_clone_arg,
)
from cliffracer.core.health_listener import HealthListener
from tests.conftest import broker_url
from tests.repo.test_class_complexity_invariants import (
    check_empty_logging_functions,
    check_source_complexity,
    count_ast_statements,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SECRET = "a_super_secret_key_that_is_at_least_32_characters_long"


# ==============================================================================
# 1. Adversarial Token Revocation Testing
# ==============================================================================


class TestAdversarialTokenRevocation:
    @pytest.fixture
    def auth_service(self) -> SimpleAuthService:
        cfg = AuthConfig(secret_key=SECRET, token_expiry_hours=1)
        svc = SimpleAuthService(cfg)
        svc.create_user("charlie", "charlie@example.com", "SecretPass123!", roles={"user"})
        return svc

    def test_every_revoked_token_is_rejected_by_validate_and_refresh(
        self, auth_service: SimpleAuthService
    ) -> None:
        """Fifty tokens for one user, each revoked, each then refused.

        Sequential on purpose. `_revoked_jtis` is a plain set updated with
        `set.add`, and no thread schedule tried here made a lost update
        observable, so a thread pool around it tested nothing about
        synchronisation.
        """
        raw_tokens = [auth_service.authenticate("charlie", "SecretPass123!") for _ in range(50)]
        tokens: list[str] = [t for t in raw_tokens if t is not None]
        assert len(set(tokens)) == 50

        for t in tokens:
            assert auth_service.validate_token(t) is not None

        for t in tokens:
            assert auth_service.revoke_token(t) is True

        assert len(auth_service._revoked_jtis) == 50
        for t in tokens:
            assert auth_service.validate_token(t) is None
            assert auth_service.refresh_token(t) is None

    def test_revoking_expired_token_succeeds_without_crash(
        self, auth_service: SimpleAuthService
    ) -> None:
        """Challenge: Revoking an already expired token must decode with verify_exp=False; nothing is kept."""
        expired_payload = {
            "user_id": "u-exp",
            "username": "charlie",
            "email": "charlie@example.com",
            "roles": [],
            "permissions": [],
            "exp": (datetime.now(UTC) - timedelta(days=1)).timestamp(),
            "iat": (datetime.now(UTC) - timedelta(days=2)).timestamp(),
            "jti": "jti-expired-challenge-99",
        }
        token = jwt.encode(expired_payload, SECRET, algorithm="HS256")

        # Must not raise ExpiredSignatureError
        assert auth_service.revoke_token(token) is True
        assert "jti-expired-challenge-99" not in auth_service._revoked_jtis

    def test_revoked_token_cannot_be_refreshed(self, auth_service: SimpleAuthService) -> None:
        """Challenge: Ensure a revoked token cannot bypass access control via refresh_token()."""
        token = auth_service.authenticate("charlie", "SecretPass123!")
        assert token is not None

        auth_service.revoke_token(token)
        refreshed = auth_service.refresh_token(token)
        assert refreshed is None, "Revoked token was unexpectedly refreshed!"

    @pytest.mark.parametrize(
        "malformed_token",
        [
            "",
            "not-a-token",
            "a.b",
            "a.b.c",
            "header.payload.invalidsig",
            None,
            12345,
            {"token": "fake"},
            b"bytes_token",
        ],
    )
    def test_a_malformed_token_passed_to_revoke_token_reports_failure_and_revokes_nothing(
        self, auth_service: SimpleAuthService, malformed_token: Any
    ) -> None:
        """A malformed input neither raises nor lands in the revoked set, and the
        return value says the revocation did not happen."""
        assert auth_service.revoke_token(malformed_token) is False
        assert auth_service._revoked_jtis == {}


# ==============================================================================
# 2. Adversarial Extension Isolation
# ==============================================================================


class TestAdversarialExtensionIsolation:
    def test_custom_object_nested_mutable_state_cloned(self) -> None:
        """Challenge: Verify arbitrary custom objects with nested mutable structures are deep-copied."""

        class CustomStateHolder:
            def __init__(self) -> None:
                self.metrics: dict[str, list[int]] = {"latencies": [10, 20]}
                self.tags: set[str] = {"v1", "test"}

        holder = CustomStateHolder()
        cloned = _safe_clone_arg(holder)

        assert cloned is not holder
        assert cloned.metrics is not holder.metrics
        assert cloned.metrics["latencies"] is not holder.metrics["latencies"]
        assert cloned.tags is not holder.tags

        cloned.metrics["latencies"].append(999)
        cloned.tags.add("modified")

        assert 999 not in holder.metrics["latencies"]
        assert "modified" not in holder.tags

    def test_service_instance_isolation_no_state_leak(self) -> None:
        """Challenge: State mutation in Service Instance A must not leak to Service Instance B."""

        class DependencyContainer:
            def __init__(self, items: list[str]) -> None:
                self.items = items

        class StateExtension(Extension):
            def __init__(self, container: DependencyContainer) -> None:
                self.container = container

        class SampleService(CliffracerService):
            ext = StateExtension(DependencyContainer(["item-1"]))

        svcA = SampleService(ServiceConfig(name="svc-a"))
        svcB = SampleService(ServiceConfig(name="svc-b"))

        assert svcA.ext is not svcB.ext
        assert svcA.ext.container is not svcB.ext.container
        assert svcA.ext.container.items is not svcB.ext.container.items

        svcA.ext.container.items.append("leaked-mutation")
        assert "leaked-mutation" not in svcB.ext.container.items

    def test_uncopyable_objects_fallback_gracefully(self) -> None:
        """Challenge: Uncopyable objects (locks, active threads) raise ExtensionIsolationError unless wrapped."""
        lock = threading.Lock()
        with pytest.raises(ExtensionIsolationError, match="Cannot isolate extension argument"):
            _safe_clone_arg(lock)

        shared_lock = SharedDependency(lock)
        cloned_lock = _safe_clone_arg(shared_lock)
        assert cloned_lock is lock

        thread = threading.Thread(target=lambda: None)
        with pytest.raises(ExtensionIsolationError, match="Cannot isolate extension argument"):
            _safe_clone_arg(thread)

        shared_thread = SharedDependency(thread)
        cloned_thread = _safe_clone_arg(shared_thread)
        assert cloned_thread is thread

        class CrashOnCopy:
            def __deepcopy__(self, memo: Any) -> Any:
                raise RuntimeError("Forbidden copy operation")

        crash_obj = CrashOnCopy()
        with pytest.raises(ExtensionIsolationError, match="Cannot isolate extension argument"):
            _safe_clone_arg(crash_obj)

        shared_crash = SharedDependency(crash_obj)
        cloned_crash = _safe_clone_arg(shared_crash)
        assert cloned_crash is crash_obj


# ==============================================================================
# 3. Adversarial Collocation & Health Probes
# ==============================================================================


class TestAdversarialCollocationSafety:
    @pytest.mark.asyncio
    async def test_terminating_connection_in_service_a_does_not_kill_service_b_or_exit(
        self,
    ) -> None:
        """Challenge: Verify os._exit is NOT called on connection closure, and that closing A's
        connection stops A and never reaches B's stop path."""
        cfg_a = ServiceConfig(name="collocated-a", health_port=0, exit_on_closed=True)
        cfg_b = ServiceConfig(name="collocated-b", health_port=0, exit_on_closed=True)

        svc_a = CliffracerService(cfg_a)
        svc_b = CliffracerService(cfg_b)

        svc_a._running = True
        svc_b._running = True
        # B's own stop path, replaced by a spy: A's closure must never get there.
        svc_b.container.lifecycle.stop = AsyncMock()  # type: ignore[method-assign]

        class FakeClosedNc:
            is_closed = True

            async def close(self) -> None:
                pass

            async def drain(self) -> None:
                pass

        svc_a.nc = FakeClosedNc()  # type: ignore[assignment]
        svc_b.nc = FakeClosedNc()  # type: ignore[assignment]

        with patch("os._exit") as mock_exit:
            # Trigger connection loss on service A
            await svc_a.container.connection._closed_callback()
            assert not mock_exit.called, "os._exit was called! Collocation footgun detected."

        # Service A stopped
        assert not svc_a._running
        health_a = await svc_a.health_check()
        assert health_a["status"] == "stopped"

        # Service B was not touched: A's closure called A's stop, and B's was never entered.
        svc_b.container.lifecycle.stop.assert_not_awaited()
        assert svc_b._running, "Service B was erroneously stopped when Service A closed!"

    @pytest.mark.asyncio
    async def test_health_listener_port_collision_raises_eaddrinuse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Challenge: Starting two HealthListeners on the same port immediately raises OSError(EADDRINUSE)."""
        monkeypatch.setattr(HealthListener, "_test_port_override", None)

        svc1 = CliffracerService(ServiceConfig(name="svc1", health_port=0))
        hl1 = HealthListener(svc1, "127.0.0.1", 0)
        await hl1.start()
        port = hl1.port
        assert port is not None and port > 0

        try:
            svc2 = CliffracerService(ServiceConfig(name="svc2", health_port=port))
            hl2 = HealthListener(svc2, "127.0.0.1", port)
            with pytest.raises(OSError) as exc_info:
                await hl2.start()
            assert exc_info.value.errno == errno.EADDRINUSE
        finally:
            await hl1.stop()


# ==============================================================================
# 4. Adversarial AST Linters & Complexity
# ==============================================================================


class TestAdversarialASTComplexityLinters:
    def test_class_statement_boundary_is_exactly_at_the_ceiling(self) -> None:
        """Challenge: a class of exactly `ceiling` statements passes; one more fails."""
        # 250 methods * 2 statements each = 500 statements
        methods_250 = "\n".join(f"    def m_{i}(self):\n        pass" for i in range(250))
        code_500 = f"class Boundary500:\n{methods_250}\n"
        tree_500 = ast.parse(code_500)
        assert count_ast_statements(tree_500.body[0]) == 500
        violations_500 = check_source_complexity(code_500, ceiling=500)
        assert len(violations_500) == 0

        # One more statement: exactly 501, the first count over the ceiling. The count is asserted
        # so the fixture cannot drift off the boundary (an earlier version of this was 503).
        code_501 = f"class Boundary501:\n{methods_250}\n    extra = 1\n"
        assert count_ast_statements(ast.parse(code_501).body[0]) == 501
        violations_501 = check_source_complexity(code_501, ceiling=500)
        assert len(violations_501) == 1
        assert violations_501[0].class_name == "Boundary501"
        assert violations_501[0].statement_count == 501

    def test_synthetic_empty_logging_functions_flagged_and_genuine_pass(self) -> None:
        """Challenge: Synthetic empty logging functions across levels are flagged; side-effect functions pass."""
        for level in ["debug", "info", "warning", "error", "critical", "exception"]:
            fake_stub = f"""
def stub_{level}():
    \"\"\"Docstring\"\"\"
    logger.{level}("Processing completed")
"""
            violations = check_empty_logging_functions(fake_stub)
            assert len(violations) == 1
            assert violations[0].function_name == f"stub_{level}"

        # A log line plus a constant return is still a stub: the return tells a
        # caller nothing the function computed.
        constant_return = """
def valid_return():
    logger.info("msg")
    return True
"""
        stub_violations = check_empty_logging_functions(constant_return)
        assert len(stub_violations) == 1
        assert stub_violations[0].function_name == "valid_return"

        genuine_functions = """
def valid_assignment():
    logger.info("msg")
    status = "done"

def valid_call():
    logger.info("msg")
    execute_side_effect()
"""
        assert len(check_empty_logging_functions(genuine_functions)) == 0


# ==============================================================================
# 5. Adversarial Benchmark Regression Checker
# ==============================================================================


class TestAdversarialBenchmarkRegressionChecker:
    @pytest.fixture
    def baseline_path(self) -> Path:
        path = REPO_ROOT / "benchmark_baseline.json"
        assert path.is_file(), "benchmark_baseline.json must exist in repo root"
        return path

    def test_baseline_self_check_succeeds(self, baseline_path: Path) -> None:
        """Challenge: Baseline checked against itself must pass with exit code 0."""
        res = subprocess.run(
            [
                sys.executable,
                "scripts/check_benchmark_regression.py",
                "--baseline",
                str(baseline_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0
        assert "SUCCESS" in res.stdout

    def test_throughput_regression_exceeding_threshold_fails_with_exit_code_1(
        self, baseline_path: Path
    ) -> None:
        """Challenge: > 15% drop in throughput triggers exit code 1."""
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        orig_tp = data["metrics"]["rpc"]["concurrency_10"]["throughput_msgs_sec"]
        data["metrics"]["rpc"]["concurrency_10"]["throughput_msgs_sec"] = orig_tp * 0.80  # -20%

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            current_path = Path(f.name)

        try:
            res = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_benchmark_regression.py",
                    "--baseline",
                    str(baseline_path),
                    "--current",
                    str(current_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 1
            assert "FAILURE" in res.stdout
            assert "rpc.concurrency_10.throughput_msgs_sec" in res.stdout
        finally:
            current_path.unlink()

    def test_latency_regression_exceeding_threshold_fails_with_exit_code_1(
        self, baseline_path: Path
    ) -> None:
        """Challenge: > 15% increase in latency exceeding calibrated noise floor triggers exit code 1."""
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        orig_lat = data["metrics"]["rpc"]["concurrency_1000"]["p50_latency_ms"]
        data["metrics"]["rpc"]["concurrency_1000"]["p50_latency_ms"] = (
            orig_lat * 1.50
        )  # +50% (>3.5ms diff)

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            current_path = Path(f.name)

        try:
            res = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_benchmark_regression.py",
                    "--baseline",
                    str(baseline_path),
                    "--current",
                    str(current_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 1
            assert "FAILURE" in res.stdout
            assert "rpc.concurrency_1000.p50_latency_ms" in res.stdout
        finally:
            current_path.unlink()

    def _run_with_small_latency_delta(
        self, baseline_path: Path, delta_ms: float, *extra_args: str
    ) -> subprocess.CompletedProcess[str]:
        """Run the gate on the baseline with `rpc.concurrency_10.p50_latency_ms` raised by `delta_ms`.

        That metric's baseline is about 1 ms, so a few tenths of a millisecond is far over the 15%
        threshold while far under the 3.5 ms floor, which makes the 0.5 ms default floor the only
        thing that can absolve it. The precondition is asserted, so a baseline that moves the
        metric until the threshold absolves the delta fails here by name rather than quietly
        stopping the test from reaching the floor.
        """
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        orig = data["metrics"]["rpc"]["concurrency_10"]["p50_latency_ms"]
        assert delta_ms / orig > 0.15, (
            f"+{delta_ms} ms on a baseline of {orig} ms is inside the 15% threshold: "
            "the noise floor no longer decides this test"
        )
        data["metrics"]["rpc"]["concurrency_10"]["p50_latency_ms"] = orig + delta_ms

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            current_path = Path(f.name)

        try:
            return subprocess.run(
                [
                    sys.executable,
                    "scripts/check_benchmark_regression.py",
                    "--baseline",
                    str(baseline_path),
                    "--current",
                    str(current_path),
                    *extra_args,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            current_path.unlink()

    def test_latency_within_calibrated_noise_floor_passes(self, baseline_path: Path) -> None:
        """+0.4 ms on a ~1 ms latency is over 15% and under the 0.5 ms floor: only the floor passes it."""
        res = self._run_with_small_latency_delta(baseline_path, 0.4)

        assert res.returncode == 0, res.stdout
        assert "SUCCESS" in res.stdout

    def test_latency_just_over_the_noise_floor_fails(self, baseline_path: Path) -> None:
        """The control: +0.6 ms is past the floor and over 15%, so the same gate says no."""
        res = self._run_with_small_latency_delta(baseline_path, 0.6)

        assert res.returncode == 1, res.stdout
        assert "rpc.concurrency_10.p50_latency_ms" in res.stdout

    def test_the_noise_floor_flag_reaches_the_comparison(self, baseline_path: Path) -> None:
        """`--noise-floor-ms` is what the comparison uses: +0.6 ms passes under a 1.0 ms floor."""
        res = self._run_with_small_latency_delta(baseline_path, 0.6, "--noise-floor-ms", "1.0")

        assert res.returncode == 0, res.stdout

    def test_hardware_specification_change_is_not_scored(self, baseline_path: Path) -> None:
        """A run on different hardware is refused, regression or not.

        A comparison across machines reports the machine as a change in the
        code, so it is neither a regression nor a pass: exit 2, the refusal code.
        """
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        # Induce severe throughput regression (>50% reduction)
        orig_tp = data["metrics"]["rpc"]["concurrency_10"]["throughput_msgs_sec"]
        data["metrics"]["rpc"]["concurrency_10"]["throughput_msgs_sec"] = orig_tp * 0.40

        # Alter host machine runner specifications
        if "environment" not in data:
            data["environment"] = {}
        if "runner" not in data["environment"]:
            data["environment"]["runner"] = {}
        data["environment"]["runner"]["cpu_count"] = (
            data["environment"]["runner"].get("cpu_count", 4) + 8
        )
        data["environment"]["runner"]["total_ram_gb"] = (
            data["environment"]["runner"].get("total_ram_gb", 16.0) + 32.0
        )

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            current_path = Path(f.name)

        try:
            res = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_benchmark_regression.py",
                    "--baseline",
                    str(baseline_path),
                    "--current",
                    str(current_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 2, res.stdout + res.stderr
            assert "NOT SCORED" in res.stderr
            assert "environment.runner.cpu_count" in res.stderr
            assert "regressed beyond" not in res.stdout
        finally:
            current_path.unlink()

    def test_missing_key_metric_fails_with_exit_code_1(self, baseline_path: Path) -> None:
        """Verify that omitting a required key SLA metric triggers a hard gate failure."""
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        # Drop a key metric from current run
        del data["metrics"]["auth"]["token_validation_ops_sec"]

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            current_path = Path(f.name)

        try:
            res = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_benchmark_regression.py",
                    "--baseline",
                    str(baseline_path),
                    "--current",
                    str(current_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 1
            assert "FAILURE" in res.stderr or "FAILURE" in res.stdout
            assert "auth.token_validation_ops_sec" in (res.stderr + res.stdout)
        finally:
            current_path.unlink()

    def test_benchmark_failures_flag_exits_with_code_1(self, baseline_path: Path) -> None:
        """Verify that explicit component execution failures in the benchmark output fail the gate."""
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        data["failures"] = {"auth": "RuntimeError: Extension crashed during benchmark"}

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            current_path = Path(f.name)

        try:
            res = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_benchmark_regression.py",
                    "--baseline",
                    str(baseline_path),
                    "--current",
                    str(current_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 1
            assert "FAILURE" in res.stderr or "FAILURE" in res.stdout
            assert "Benchmark run contains execution failures" in (res.stderr + res.stdout)
        finally:
            current_path.unlink()


# ==============================================================================
# 6. Adversarial Benchmark Aggregator & Environment Probing
# ==============================================================================


class TestAdversarialBenchmarkAggregatorAndProbes:
    def test_get_environment_context_probes_varz_dynamically(self) -> None:
        """Verify get_environment_context dynamically derives jetstream and max_payload from varz."""
        from tests.benchmark.benchmarks import get_environment_context

        mock_varz_data = {
            "version": "2.11.5",
            "server_id": "TEST_SRV_99",
            "jetstream": {"config": {"max_memory": 1024}},
            "max_payload": 4194304,
            "mem": 1234567,
            "cpu": 12.5,
            "max_connections": 10000,
            "connections": 42,
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_varz_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            env = get_environment_context(broker_url())

        nats = env["nats"]
        assert nats["version"] == "2.11.5"
        assert nats["server_id"] == "TEST_SRV_99"
        assert nats["jetstream_enabled"] is True
        assert nats["max_payload_bytes"] == 4194304
        assert "probe_failed" not in nats

    def test_get_environment_context_jetstream_disabled(self) -> None:
        """Verify jetstream_enabled evaluates to False when varz jetstream field is absent."""
        from tests.benchmark.benchmarks import get_environment_context

        mock_varz_data = {
            "version": "2.10.29",
            "server_id": "TEST_SRV_NO_JS",
            "max_payload": 1048576,
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_varz_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            env = get_environment_context(broker_url())

        assert env["nats"]["jetstream_enabled"] is False

    def test_get_environment_context_probe_failure_records_marker_without_fabrications(
        self,
    ) -> None:
        """Verify probe failure omits fabricated version/jetstream/payload numbers and records probe_failed."""
        from urllib.error import URLError

        from tests.benchmark.benchmarks import get_environment_context

        with patch("urllib.request.urlopen", side_effect=URLError("Connection refused")):
            env = get_environment_context(broker_url())

        nats = env["nats"]
        assert "probe_failed" in nats
        assert "Connection refused" in nats["probe_failed"]
        assert "version" not in nats
        assert "jetstream_enabled" not in nats
        assert "max_payload_bytes" not in nats

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_broker_monitor")
    async def test_run_all_benchmarks_flags_extension_crash_without_skipping(self) -> None:
        """Verify extension crashes in run_all_benchmarks record failures without swallow-and-skip."""
        from tests.benchmark.benchmarks import run_all_benchmarks

        with (
            patch("tests.benchmark.benchmarks.benchmark_rpc", new_callable=AsyncMock) as m_rpc,
            patch("tests.benchmark.benchmarks.benchmark_jetstream", new_callable=AsyncMock) as m_js,
            patch("tests.benchmark.benchmarks.benchmark_serialization", return_value={}),
            patch(
                "tests.benchmark.benchmarks.benchmark_auth",
                side_effect=RuntimeError("Auth extension crashed"),
            ),
            patch("tests.benchmark.benchmarks.benchmark_kv", new_callable=AsyncMock) as m_kv,
        ):
            m_rpc.return_value = {}
            m_js.return_value = {}
            m_kv.return_value = {}

            res = await run_all_benchmarks(include_extensions=True)

        assert "failures" in res
        assert "auth" in res["failures"]
        assert "Auth extension crashed" in res["failures"]["auth"]
        assert res["metrics"]["auth"]["failed"] is True
        assert "skipped" not in res["metrics"]["auth"]

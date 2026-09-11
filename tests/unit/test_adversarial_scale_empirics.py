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
import concurrent.futures
import errno
import json
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
from tests.unit.test_class_complexity_invariants import (
    check_empty_logging_functions,
    check_source_complexity,
    count_ast_statements,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SECRET = "a_super_secret_key_that_is_at_least_32_characters_long"


# ==============================================================================
# 1. Adversarial Token Revocation Testing
# ==============================================================================


@pytest.mark.unit
class TestAdversarialTokenRevocation:
    @pytest.fixture
    def auth_service(self) -> SimpleAuthService:
        cfg = AuthConfig(secret_key=SECRET, token_expiry_hours=1)
        svc = SimpleAuthService(cfg)
        svc.create_user("charlie", "charlie@example.com", "SecretPass123!", roles={"user"})
        return svc

    def test_concurrent_token_revocations(self, auth_service: SimpleAuthService) -> None:
        """Challenge: Concurrently mint 50 tokens and revoke them across 10 threads."""
        raw_tokens = [auth_service.authenticate("charlie", "SecretPass123!") for _ in range(50)]
        assert all(isinstance(t, str) for t in raw_tokens)
        tokens: list[str] = [t for t in raw_tokens if t is not None]
        assert len(set(tokens)) == 50

        # Validate all initially succeed
        for t in tokens:
            assert auth_service.validate_token(t) is not None

        # Concurrently revoke
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(auth_service.revoke_token, tokens))

        # Validate all tokens are now rejected
        for t in tokens:
            assert auth_service.validate_token(t) is None
            assert auth_service.refresh_token(t) is None

    def test_revoking_expired_token_succeeds_without_crash(
        self, auth_service: SimpleAuthService
    ) -> None:
        """Challenge: Revoking an already expired token must decode with verify_exp=False and register JTI."""
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
        auth_service.revoke_token(token)
        assert "jti-expired-challenge-99" in auth_service._revoked_jtis

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
    def test_malformed_tokens_passed_to_revoke_token_survive(
        self, auth_service: SimpleAuthService, malformed_token: Any
    ) -> None:
        """Challenge: Non-standard / malformed inputs must be handled safely without unhandled crashes."""
        try:
            auth_service.revoke_token(malformed_token)
        except Exception as exc:
            pytest.fail(f"revoke_token crashed on {malformed_token!r}: {exc}")


# ==============================================================================
# 2. Adversarial Extension Isolation
# ==============================================================================


@pytest.mark.unit
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


@pytest.mark.unit
class TestAdversarialCollocationSafety:
    @pytest.mark.asyncio
    async def test_terminating_connection_in_service_a_does_not_kill_service_b_or_exit(
        self,
    ) -> None:
        """Challenge: Verify os._exit is NOT called on connection closure and collocated Service B stays running."""
        cfg_a = ServiceConfig(name="collocated-a", health_port=0, exit_on_closed=True)
        cfg_b = ServiceConfig(name="collocated-b", health_port=0, exit_on_closed=True)

        svc_a = CliffracerService(cfg_a)
        svc_b = CliffracerService(cfg_b)

        svc_a._running = True
        svc_b._running = True

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

        # Service B is still intact and running
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


@pytest.mark.unit
class TestAdversarialASTComplexityLinters:
    def test_class_statement_boundary_500_vs_501(self) -> None:
        """Challenge: 500 statements passes ceiling; 501 fails unless @override_length_check decorated."""
        # 250 methods * 2 statements each = 500 statements
        methods_250 = "\n".join(f"    def m_{i}(self):\n        pass" for i in range(250))
        code_500 = f"class Boundary500:\n{methods_250}\n"
        tree_500 = ast.parse(code_500)
        assert count_ast_statements(tree_500.body[0]) == 500
        violations_500 = check_source_complexity(code_500, ceiling=500)
        assert len(violations_500) == 0

        # Add 1 extra method with 2 statements -> 503 > 500
        code_501 = f"class Boundary501:\n{methods_250}\n    def extra(self):\n        x = 1\n        return x\n"
        violations_501 = check_source_complexity(code_501, ceiling=500)
        assert len(violations_501) == 1
        assert violations_501[0].class_name == "Boundary501"

        # Adding valid override clears violation
        code_exempt = (
            "from cliffracer.invariants import override_length_check\n\n"
            "@override_length_check(reason='Approved state machine consolidation')\n"
            f"{code_501}"
        )
        assert len(check_source_complexity(code_exempt, ceiling=500)) == 0

    def test_override_length_check_rejects_empty_and_whitespace_reasons(self) -> None:
        """Challenge: Reject empty or blank whitespace reason strings."""
        methods = "\n".join(f"    def m_{i}(self):\n        pass" for i in range(260))
        for invalid_reason in ["", "   ", "\t\n "]:
            code = (
                f"@override_length_check(reason={invalid_reason!r})\n"
                f"class InvalidClass:\n{methods}\n"
            )
            violations = check_source_complexity(code, ceiling=500)
            assert len(violations) == 1, f"Failed to reject reason {invalid_reason!r}"

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

        genuine_functions = """
def valid_return():
    logger.info("msg")
    return True

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


@pytest.mark.unit
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

    def test_latency_within_calibrated_noise_floor_passes(self, baseline_path: Path) -> None:
        """Challenge: Latency delta within 3.5ms calibrated noise floor passes cleanly."""
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        orig_lat = data["metrics"]["rpc"]["concurrency_1000"]["p50_latency_ms"]
        data["metrics"]["rpc"]["concurrency_1000"]["p50_latency_ms"] = (
            orig_lat + 2.0
        )  # +2.0ms < 3.5ms noise floor

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
            assert res.returncode == 0
            assert "SUCCESS" in res.stdout
        finally:
            current_path.unlink()

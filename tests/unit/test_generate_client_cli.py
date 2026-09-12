"""Tests verifying exit codes and URL resolution of cliffracer-generate-client."""

import shutil
import subprocess
import sys

import pytest

from cliffracer.generate_client.cli import main, resolve_nats_url

pytestmark = pytest.mark.unit


def test_url_precedence_flag_env_default():
    assert resolve_nats_url("nats://a:1", {"CLIFFRACER_NATS_URL": "nats://b:2"}) == "nats://a:1"
    assert resolve_nats_url(None, {"CLIFFRACER_NATS_URL": "nats://b:2"}) == "nats://b:2"
    assert resolve_nats_url(None, {}) == "nats://localhost:4222"


def test_class_mode_writes_a_client(tmp_path):
    out = tmp_path / "c.py"
    rc = main(
        [
            "--class",
            "tests.unit.test_introspect:Orders",
            "--service",
            "orders",
            "--version",
            "1",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    assert "class OrdersClient(ServiceClient)" in out.read_text()


def test_exit_5_when_the_class_cannot_be_imported(capsys):
    rc = main(["--class", "no.such:Thing", "--service", "x"])
    assert rc == 5
    assert "no.such" in capsys.readouterr().err


def test_exit_3_when_no_broker(capsys):
    rc = main(["--service", "x", "--nats-url", "nats://127.0.0.1:1", "--timeout", "0.5"])
    assert rc == 3
    assert "nats://127.0.0.1:1" in capsys.readouterr().err


def test_exit_4_via_class_mode_for_an_unemittable_type(capsys, tmp_path):
    """A model a generated client could not import. The service runs fine; only
    the generator refuses, which is why this is the command's rule and not a
    refuse-to-start one."""
    mod = tmp_path / "svc_mod.py"
    mod.write_text(
        "from pydantic import BaseModel\n"
        "from cliffracer import CliffracerService, rpc\n"
        "class X(BaseModel):\n"
        "    a: int\n"
        "X.__module__ = '__main__'\n"
        "class S(CliffracerService):\n"
        "    @rpc\n"
        "    async def f(self, x: X) -> int:\n"
        "        return 1\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        rc = main(["--class", "svc_mod:S", "--service", "s"])
    finally:
        sys.path.remove(str(tmp_path))
    assert rc == 4
    assert "__main__" in capsys.readouterr().err


def test_exit_4_when_a_handler_is_not_annotated(capsys, tmp_path):
    """The other way to be undescribable, and the one a service author hits."""
    mod = tmp_path / "untyped_mod.py"
    mod.write_text(
        "from cliffracer import CliffracerService, rpc\n"
        "class S(CliffracerService):\n"
        "    @rpc\n"
        "    async def f(self, x):\n"
        "        return x\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        rc = main(["--class", "untyped_mod:S", "--service", "s"])
    finally:
        sys.path.remove(str(tmp_path))
    assert rc == 4
    err = capsys.readouterr().err
    assert "S.f" in err and "x" in err


def test_the_console_script_is_installed():
    exe = shutil.which("cliffracer-generate-client")
    assert exe, "console script not installed: run uv sync"
    assert subprocess.run([exe, "--help"], capture_output=True).returncode == 0


def test_never_writes_a_partial_file(tmp_path):
    """A half-written client is worse than none: it imports, and it lies."""
    out = tmp_path / "c.py"
    main(["--class", "no.such:Thing", "--service", "x", "--out", str(out)])
    assert not out.exists()


def test_exit_4_when_service_name_is_invalid(capsys, tmp_path):
    # Invalid service name on CLI returns exit code 4.
    ret = main(
        [
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "order.service",
            "--out",
            str(tmp_path / "out.py"),
        ]
    )
    assert ret == 4
    _, err = capsys.readouterr()
    assert "order.service" in err


def test_exit_4_when_model_is_parametrized_generic(capsys, tmp_path):
    # Parametrized generic model returns exit code 4.
    ret = main(
        [
            "--class",
            "tests.fixtures.typed_client.service_generic:GenericService",
            "--service",
            "generic-service",
            "--out",
            str(tmp_path / "out.py"),
        ]
    )
    assert ret == 4
    _, err = capsys.readouterr()
    assert "Page[int]" in err
    assert "Move these models into an importable package" in err


def test_exit_2_when_service_request_times_out(monkeypatch, capsys):
    """Verify fetch_description timeout returns exit code 2."""
    import nats.errors

    async def mock_fetch(*args, **kwargs):
        raise nats.errors.TimeoutError()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", mock_fetch)
    rc = main(["--service", "slow_svc", "--nats-url", "nats://127.0.0.1:4222", "--timeout", "1.0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "slow_svc" in err
    assert "answered on the describe subject within 1.0s" in err


def test_exit_2_when_no_responders(monkeypatch, capsys):
    """Verify fetch_description NoRespondersError returns exit code 2."""
    import nats.errors

    async def mock_fetch(*args, **kwargs):
        raise nats.errors.NoRespondersError()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", mock_fetch)
    rc = main(["--service", "nobody", "--nats-url", "nats://127.0.0.1:4222", "--timeout", "1.0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "nobody" in err
    assert "answered on the describe subject within 1.0s" in err

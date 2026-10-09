"""End-to-end integration tests for typed client generation, verification, and RPC calls."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import (
    ClientOutOfDate,
    RpcRefused,
    RpcServerError,
    RpcValidationError,
)
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import RPCError as CoreRPCError
from cliffracer.generate_client.cli import describe_subject
from conftest import console_script
from tests.fixtures.typed_client import service as fixture
from tests.fixtures.typed_client.consumer import Consumer
from tests.fixtures.typed_client.models import Line, Order, Receipt

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


SERVICE = "warehouse_e2e"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
async def warehouse():
    svc = fixture.make(SERVICE)
    await svc.start()
    yield svc
    await svc.stop()


@pytest.fixture
def token():
    return fixture.token()


def _tree_env() -> dict[str, str]:
    """The environment a generator subprocess runs in: it imports THIS tree.

    The console script is installed, and its interpreter resolves `cliffracer`
    through whatever the environment installed, which can be last week's wheel or
    another checkout. `src` and the packages' `src` go first, so what runs is the
    tree under test, and the repository root follows for the `tests.fixtures`
    import of `--class` mode. An inherited `PYTHONPATH` is kept after them, not
    replaced: replacing it left a mutation to the generator unseen by every test
    here.
    """
    paths = [str(REPO / "src"), *(str(p) for p in sorted(REPO.glob("packages/*/src"))), str(REPO)]
    inherited = os.environ.get("PYTHONPATH")
    if inherited:
        paths.append(inherited)
    return {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}


async def _generate(mode_args: list[str], out) -> str:
    """Run the installed console script asynchronously."""
    exe = console_script("cliffracer-generate-client")
    env = _tree_env()
    proc = await asyncio.create_subprocess_exec(
        exe,
        "--service",
        SERVICE,
        "--out",
        str(out),
        *mode_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    assert proc.returncode == 0, stderr.decode()
    return out.read_text()


def _import(path):
    spec = importlib.util.spec_from_file_location("warehouse_client", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["warehouse_client"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_generator_subprocess_imports_the_tree_under_test():
    """The property every generator test below depends on, observed rather than assumed."""
    result = subprocess.run(
        [sys.executable, "-c", "import cliffracer; print(cliffracer.__file__)"],
        capture_output=True,
        text=True,
        timeout=30,
        env=_tree_env(),
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve().is_relative_to(REPO / "src")


async def test_1_the_service_works_by_direct_calls(warehouse, token, nats_connection):
    """Before generating anything: the service is real, refuses without a token,
    answers with one, and refuses a bad payload."""
    caller = CliffracerService(ServiceConfig(name="direct_caller"))
    await caller.start()
    try:
        # call_rpc raises core RPCError; a generated client call raises ClientError.
        with pytest.raises(CoreRPCError, match="refused: unauthenticated"):
            await caller.call_rpc(SERVICE, "create", order={"lines": [{"sku": "a"}]})

        reply = await nats_connection.request(
            HandlerDiscovery.outbound_subject(caller.config, SERVICE, "rpc", "create"),
            b'{"order": {"lines": [{"sku": "a", "qty": 2}]}}',
            timeout=2,
            headers={"authorization": f"bearer {token}"},
        )
        body = json.loads(reply.data)
        assert body["success"] is True
        assert body["result"]["total_qty"] == 2

        bad = await nats_connection.request(
            HandlerDiscovery.outbound_subject(caller.config, SERVICE, "rpc", "create"),
            b'{"order": {"lines": "nope"}}',
            timeout=2,
            headers={"authorization": f"bearer {token}"},
        )
        assert json.loads(bad.data)["error"] == "validation failed"
    finally:
        await caller.stop()


async def test_2_no_service_answers_is_exit_2(nats_connection, tmp_path):
    """The broker answers and no service does: exit 2, naming the service, writing nothing.

    The other half of the pair, exit 3 for a broker that cannot be reached, is pinned by
    `tests/unit/test_generate_client_cli.py::test_exit_3_when_no_broker`; this test
    does not exercise it."""
    exe = console_script("cliffracer-generate-client")
    out = tmp_path / "x.py"
    result = subprocess.run(
        [
            exe,
            "--service",
            "nobody_home",
            "--nats-url",
            nats_connection.connected_url.geturl(),
            "--timeout",
            "1",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=_tree_env(),
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "nobody_home" in result.stderr
    assert not out.exists()


async def test_2_slow_service_times_out_is_exit_2(nats_connection, tmp_path):
    """Verify timed out requests return exit code 2."""
    sub = await nats_connection.subscribe(describe_subject("slow_poke"))
    exe = console_script("cliffracer-generate-client")
    out = tmp_path / "slow.py"
    try:
        result = subprocess.run(
            [
                exe,
                "--service",
                "slow_poke",
                "--nats-url",
                nats_connection.connected_url.geturl(),
                "--timeout",
                "0.5",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=_tree_env(),
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert "slow_poke" in result.stderr
        assert "answered on the describe subject within 0.5s" in result.stderr
        assert not out.exists()
    finally:
        await sub.unsubscribe()


async def test_3_live_and_class_generation_are_byte_identical(warehouse, token, tmp_path):
    """The two modes are one function over one class. If they could differ, a
    client generated in CI and a client generated against staging would too.

    The live mode needs the header: this service has `AuthExtension` in front of
    it, so it refuses an unauthenticated describe like any other message. That
    is the reason `--header` exists -- without it no service behind auth can be
    described at all, which the plan did not account for.
    """
    live = await _generate(
        [
            "--nats-url",
            warehouse.config.nats_url,
            "--header",
            f"authorization=bearer {token}",
        ],
        tmp_path / "live.py",
    )
    # Only the class invocation is told the version. The live one reads it off
    # the running service's config, so equal bytes mean the two agree.
    from_class = await _generate(
        ["--class", "tests.fixtures.typed_client.service:Warehouse", "--version", "3.1.4"],
        tmp_path / "cls.py",
    )

    assert live == from_class
    assert "class WarehouseE2eClient(ServiceClient)" in live


async def test_4_a_second_service_uses_the_generated_client(warehouse, token, tmp_path):
    path = tmp_path / "warehouse_client.py"
    await _generate(["--class", "tests.fixtures.typed_client.service:Warehouse"], path)
    mod = _import(path)

    consumer = Consumer(
        mod.WarehouseE2eClient,
        service=SERVICE,
        headers={"authorization": f"bearer {token}"},
    )
    await consumer.start()
    try:
        receipt = await consumer.warehouse.create(
            Order(lines=[Line(sku="a", qty=2), Line(sku="b")], priority="high"), note="rush"
        )
        assert receipt == Receipt(order_id="o-2", total_qty=3, tags={"note": "rush"})

        assert await consumer.warehouse.lines(["x", "y"]) == [Line(sku="x"), Line(sku="y")]

        # Handler invocation through generated client.
        assert await consumer.restock("z", qty=4) == "o-1"
        assert await consumer.warehouse.find("missing") is None

        # model_construct bypasses client validation to test server-side refusal.
        with (
            pytest.warns(UserWarning, match="serialized value may not be as expected"),
            pytest.raises(RpcValidationError) as caught,
        ):
            await consumer.warehouse.create(Order.model_construct(lines="nope"))
        assert any("lines" in str(d.get("loc")) for d in caught.value.details)

        # A handler that raised is the service's fault, not the caller's.
        with pytest.raises(RpcServerError, match="Internal server error"):
            await consumer.warehouse.fail("boom")

        anonymous = mod.WarehouseE2eClient(consumer.nc, service=SERVICE)
        with pytest.raises(RpcRefused):
            await anonymous.lines(["a"])
    finally:
        await consumer.stop()


async def test_5_drift_is_named_before_any_call(warehouse, token, tmp_path, nats_connection):
    """The service grows a parameter; the client says so, by name, on the first
    call -- and on a method whose own signature did not change."""
    path = tmp_path / "warehouse_client.py"
    await _generate(["--class", "tests.fixtures.typed_client.service:Warehouse"], path)
    mod = _import(path)
    await warehouse.stop()

    class Changed(fixture.Warehouse):
        @rpc
        async def create(  # type: ignore[override]
            self, order: Order, note: str = "", rush: bool = False
        ) -> Receipt:
            return Receipt(order_id="x", total_qty=0)

    changed = Changed(ServiceConfig(name=SERVICE, version="3.2.0"))
    await changed.start()
    try:
        client = mod.WarehouseE2eClient(
            nats_connection, service=SERVICE, headers={"authorization": f"bearer {token}"}
        )
        with pytest.raises(ClientOutOfDate) as caught:
            await client.lines(["a"])
        assert caught.value.changed == ["create"]
        assert caught.value.missing == []
    finally:
        await changed.stop()


async def test_a_live_orders_description_checks_the_artifact_without_rewriting_it(
    warehouse, token, tmp_path, nats_connection
):
    """An authenticated live description drives the same read-only build check."""
    out = tmp_path / "warehouse_client.py"
    mode = [
        "--nats-url",
        nats_connection.connected_url.geturl(),
        "--header",
        f"authorization=bearer {token}",
    ]
    await _generate(mode, out)
    for stale in (False, True):
        if stale:
            out.write_text(out.read_text().replace("async def find(", "async def lookup("))
        before = (out.read_bytes(), out.stat().st_ino, out.stat().st_mtime_ns)
        process = await asyncio.create_subprocess_exec(
            console_script("cliffracer-generate-client"),
            "--service",
            SERVICE,
            "--out",
            str(out),
            "--check",
            *mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_tree_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        assert process.returncode == (8 if stale else 0), stderr.decode()
        assert stdout == b""
        if stale:
            assert b"missing RPCs: find" in stderr
            assert b"extra RPCs: lookup" in stderr
        assert (out.read_bytes(), out.stat().st_ino, out.stat().st_mtime_ns) == before

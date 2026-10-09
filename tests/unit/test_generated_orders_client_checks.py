"""Checking a derived order client leaves the artifact alone and names drift."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import cliffracer
from cliffracer.generate_client.cli import main

pytestmark = pytest.mark.unit

#: The `src` directory this process imported `cliffracer` from. A child is pointed at it, after the
#: service's own directories, so it runs the code under test: without it the child imports whatever
#: the interpreter has installed, which in a scratch copy of the tree is another tree's code.
IMPORTED_SRC = str(Path(cliffracer.__file__).resolve().parents[1])

ORDERS = """from cliffracer import CliffracerService, rpc

class Orders(CliffracerService):
    @rpc
    async def create(self, quantity: int) -> str:
        return "order-a"

    @rpc
    async def cancel(self, order_id: str) -> bool:
        return True

    @rpc
    async def status(self, order_id: str) -> str:
        return "ready"
"""


@pytest.fixture
def shop(tmp_path):
    (tmp_path / "shop.py").write_text(ORDERS)
    return tmp_path


def _run(
    shop: Path,
    *extra: str,
    target: str = "shop:Orders",
    out: Path | None = None,
    imports: tuple[Path, ...] = (),
):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "cliffracer.generate_client.cli",
            "--class",
            target,
            "--service",
            "orders",
            "--version",
            "1.0.0",
            "--out",
            str(out or shop / "orders_client.py"),
            *extra,
        ],
        cwd=shop,
        env=_child_env(shop, imports),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _child_env(shop: Path, imports: tuple[Path, ...] = ()) -> dict[str, str]:
    """The child's environment: the service's directory and any it imports from first, then the
    `src` this process imported, so the service's modules are found before anything else."""
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(str(path) for path in (shop, *imports, IMPORTED_SRC)),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_a_child_imports_the_cliffracer_this_process_imported(shop):
    """Every subprocess row runs the generator in a child, so each must run the code under test."""
    done = subprocess.run(
        [sys.executable, "-c", "import cliffracer, sys; sys.stdout.write(cliffracer.__file__)"],
        cwd=shop,
        env=_child_env(shop),
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert Path(done.stdout).resolve().parents[1] == Path(IMPORTED_SRC), (done.stdout, IMPORTED_SRC)


def _snapshot(path: Path):
    info = path.stat()
    return path.read_bytes(), info.st_ino, info.st_mode, info.st_mtime_ns


def test_a_current_client_checks_without_replacing_or_rewriting_it(shop):
    assert _run(shop).returncode == 0
    out = shop / "orders_client.py"
    before = _snapshot(out)
    checked = _run(shop, "--check")
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout == checked.stderr == ""
    assert _snapshot(out) == before
    assert not list(shop.glob(".*.tmp"))


def test_contract_drift_names_missing_extra_and_changed_orders_rpcs(shop):
    assert _run(shop).returncode == 0
    out = shop / "orders_client.py"
    before = _snapshot(out)
    changed = ORDERS.replace("quantity: int", "quantity: str").replace("def cancel(", "def refund(")
    (shop / "shop.py").write_text(changed)
    checked = _run(shop, "--check")
    assert checked.returncode == 8, checked.stderr
    assert "missing RPCs: refund" in checked.stderr
    assert "extra RPCs: cancel" in checked.stderr
    assert "changed RPCs: create" in checked.stderr
    assert "status" not in checked.stderr, "an unchanged RPC was reported as stale"
    assert str(out) in checked.stderr
    assert "without --check" in checked.stderr
    assert checked.stdout == ""
    assert _snapshot(out) == before


@pytest.mark.parametrize("nested", [False, True])
def test_a_missing_client_is_reported_without_creating_it_or_its_directory(shop, nested):
    out = shop / "absent" / "orders_client.py" if nested else shop / "orders_client.py"
    checked = _run(shop, "--check", out=out)
    assert checked.returncode == 8, checked.stderr
    assert "missing generated client" in checked.stderr
    assert "missing RPCs: cancel, create, status" in checked.stderr
    assert not out.exists()
    if nested:
        assert not out.parent.exists()


@pytest.mark.parametrize("change", ["comment", "crlf", "version", "method_body", "missing_method"])
def test_check_compares_the_whole_artifact_even_when_signature_hashes_match(shop, change):
    assert _run(shop).returncode == 0
    out = shop / "orders_client.py"
    data = out.read_bytes()
    if change == "comment":
        data += b"\n# deployment note\n"
    elif change == "crlf":
        data = data.replace(b"\n", b"\r\n")
    elif change == "version":
        data = data.replace(b'VERSION = "1.0.0"', b'VERSION = "2.0.0"')
    elif change == "method_body":
        data = data.replace(b"await self._call(", b"await self._wrong_transport(")
    else:
        data = data.replace(b"async def status(", b"async def lookup(")
    assert data != out.read_bytes(), "the control must actually edit the artifact"
    out.write_bytes(data)
    before = _snapshot(out)
    checked = _run(shop, "--check")
    assert checked.returncode == 8, checked.stderr
    if change == "missing_method":
        assert "missing RPCs: status" in checked.stderr
        assert "extra RPCs: lookup" in checked.stderr
    elif change == "method_body":
        assert "changed RPCs: cancel, create, status" in checked.stderr
    else:
        assert "other generated content differs" in checked.stderr
    assert _snapshot(out) == before


@pytest.mark.parametrize(
    "data", [b"this is not python!!!", b"\xff\xfe", b"SIGNATURES = function()\n"]
)
def test_an_unreadable_contract_is_stale_without_executing_or_rewriting_it(shop, data):
    out = shop / "orders_client.py"
    out.write_bytes(data)
    before = _snapshot(out)
    checked = _run(shop, "--check")
    assert checked.returncode == 8, checked.stderr
    assert "cannot read generated-client metadata" in checked.stderr
    assert "Traceback" not in checked.stderr
    assert _snapshot(out) == before


def test_check_never_imports_the_existing_client(shop):
    assert _run(shop).returncode == 0
    out = shop / "orders_client.py"
    marker = shop / "executed-client"
    with out.open("a") as file:
        file.write(f"\n__import__('pathlib').Path({str(marker)!r}).write_text('executed')\n")
    checked = _run(shop, "--check")
    assert checked.returncode == 8, checked.stderr
    assert not marker.exists(), "checking an artifact executed its code"


def test_a_directory_is_a_read_failure_and_remains_a_directory(shop):
    out = shop / "orders_client.py"
    out.mkdir()
    checked = _run(shop, "--check")
    assert checked.returncode == 10, checked.stderr
    assert "could not read" in checked.stderr and str(out) in checked.stderr
    assert out.is_dir() and list(out.iterdir()) == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--check"], "--check requires --out"),
        (["--require-source-under", "."], "--require-source-under applies only with --class"),
        (["--check", "--require-source-under", "."], "--check requires --out"),
    ],
)
def test_invalid_check_invocations_are_usage_errors_before_contacting_a_broker(
    args, message, capsys
):
    with pytest.raises(SystemExit) as caught:
        main(["--service", "orders", *args])
    assert caught.value.code == 7
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("spelling", ["absolute", "relative", "symlink"])
def test_the_requested_checkout_generates_and_checks_a_client(shop, spelling):
    root = shop
    if spelling == "relative":
        root = Path(".")
    elif spelling == "symlink":
        root = shop / "checkout-link"
        root.symlink_to(shop, target_is_directory=True)
    made = _run(shop, "--require-source-under", str(root))
    assert made.returncode == 0, made.stderr
    before = _snapshot(shop / "orders_client.py")
    checked = _run(shop, "--check", "--require-source-under", str(root))
    assert checked.returncode == 0, checked.stderr
    assert _snapshot(shop / "orders_client.py") == before


@pytest.mark.parametrize("checking", [False, True])
def test_a_sibling_checkout_is_refused_even_when_it_generates_identical_bytes(shop, checking):
    requested = shop / "workspace"
    wrong = shop / "workspace-other"
    for root in (requested, wrong):
        root.mkdir()
        (root / "shop.py").write_text(ORDERS)
        assert _run(root).returncode == 0
    out = requested / "orders_client.py"
    assert out.read_bytes() == (wrong / "orders_client.py").read_bytes()
    before = _snapshot(out)
    args = ["--require-source-under", str(requested)] + (["--check"] if checking else [])
    refused = _run(wrong, *args, out=out)
    assert refused.returncode == 9, refused.stderr
    assert str(wrong / "shop.py") in refused.stderr
    assert str(requested) in refused.stderr and "required under" in refused.stderr
    assert refused.stdout == ""
    assert _snapshot(out) == before


@pytest.mark.parametrize("indirection", ["symlink", "reexport"])
def test_local_paths_cannot_hide_a_service_class_defined_in_another_checkout(shop, indirection):
    requested = shop / "workspace"
    wrong = shop / "workspace-other"
    requested.mkdir()
    wrong.mkdir()
    source = wrong / "warehouse.py"
    source.write_text(ORDERS)
    if indirection == "symlink":
        (requested / "shop.py").symlink_to(source)
    else:
        (requested / "shop.py").write_text("from warehouse import Orders\n")
    refused = _run(requested, "--require-source-under", str(requested), imports=(wrong,))
    assert refused.returncode == 9, refused.stderr
    assert str(source) in refused.stderr
    assert not (requested / "orders_client.py").exists()


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_a_required_source_root_must_be_an_existing_directory(shop, root_kind):
    root = shop / "unknown-checkout"
    if root_kind == "file":
        root.write_text("a file is not a checkout")
    refused = _run(shop, "--require-source-under", str(root))
    assert refused.returncode == 9, refused.stderr
    assert str(root) in refused.stderr
    assert not (shop / "orders_client.py").exists()


def test_a_class_without_verifiable_source_is_refused_before_emission(shop):
    refused = _run(shop, "--require-source-under", str(shop), target="builtins:str")
    assert refused.returncode == 9, refused.stderr
    assert "builtins:str" in refused.stderr and "cannot verify" in refused.stderr
    assert "Traceback" not in refused.stderr
    assert not (shop / "orders_client.py").exists()


def test_shared_models_can_live_outside_the_service_checkout(shop):
    requested = shop / "workspace"
    shared = shop / "shared"
    requested.mkdir()
    shared.mkdir()
    (shared / "inventory.py").write_text(
        "from pydantic import BaseModel\nclass Quantity(BaseModel):\n    units: int\n"
    )
    (requested / "shop.py").write_text(
        "from inventory import Quantity\n" + ORDERS.replace("quantity: int", "quantity: Quantity")
    )
    made = _run(requested, "--require-source-under", str(requested), imports=(shared,))
    assert made.returncode == 0, made.stderr
    assert "from inventory import Quantity" in (requested / "orders_client.py").read_text()

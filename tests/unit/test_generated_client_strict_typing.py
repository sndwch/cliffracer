"""Generated business clients and their consumers pass an independent strict gate."""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from cliffracer.generate_client.cli import main
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import Description

pytestmark = pytest.mark.unit

CONSUMER = """from orders_client import OrdersClient
from tests.fixtures.orders_client import OrderRequest, OrderReceipt

async def checkout(client: OrdersClient) -> OrderReceipt:
    receipt: OrderReceipt = await client.create(
        "retail", OrderRequest(sku="widget", quantity=2)
    )
    found: OrderReceipt | None = await client.find(receipt.order_id)
    orders: list[OrderReceipt] = await client.list_orders()
    label: str = await client.label(str=42)
    assert label == "Order 42"
    await client.cancel(receipt.order_id)
    assert found is not None
    assert orders[0].quantity == 2
    return receipt
"""


def _check(
    tmp_path: Path, source: str, consumer: str = CONSUMER, *, cache: Path
) -> subprocess.CompletedProcess[str]:
    # Fresh paths force content validation even for equal-size edits whose
    # timestamps match. Pytest owns the files and retains them for diagnostics.
    workspace = Path(tempfile.mkdtemp(prefix="check-", dir=tmp_path))
    generated = workspace / "orders_client.py"
    generated.write_text(source)
    caller = workspace / "consumer.py"
    caller.write_text(consumer)
    config = workspace / "mypy.ini"
    config.write_text("[mypy]\n")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(config),
            "--strict",
            "--cache-dir",
            str(cache),
            "--follow-imports=silent",
            str(generated),
            str(caller),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def source(tmp_path: Path) -> str:
    path = tmp_path / "orders_client.py"
    assert (
        main(
            [
                "--class",
                "tests.fixtures.orders_client:Orders",
                "--service",
                "orders",
                "--version",
                "1",
                "--out",
                str(path),
            ]
        )
        == 0
    )
    return path.read_text()


def test_generated_orders_and_typed_consumer_pass_strict_mypy(tmp_path, source, mypy_cache):
    result = _check(tmp_path, source, cache=mypy_cache)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("customer", ['"retail"', "42"])
def test_activation_binding_preserves_generated_business_types(
    tmp_path, source, mypy_cache, customer
):
    consumer = f"""from orders_client import OrdersClient
from cliffracer.runners.contracts import ActivationReference
from tests.fixtures.orders_client import OrderRequest, OrderReceipt

async def place_order(reference: ActivationReference) -> OrderReceipt:
    client = reference.bind(OrdersClient)
    return await client.create({customer}, OrderRequest(sku="bolts", quantity=2))
"""
    result = _check(tmp_path, source, consumer, cache=mypy_cache)
    assert result.returncode == (1 if customer == "42" else 0), result.stdout + result.stderr
    if customer == "42":
        assert "[arg-type]" in result.stdout


@pytest.mark.parametrize(
    ("before", "after", "diagnostic"),
    [
        ('"retail", OrderRequest', "42, OrderRequest", "[arg-type]"),
        ("receipt: OrderReceipt =", "receipt: str =", "[assignment]"),
    ],
)
def test_strict_consumer_rejects_wrong_usage(
    tmp_path, source, mypy_cache, before, after, diagnostic
):
    result = _check(tmp_path, source, CONSUMER.replace(before, after), cache=mypy_cache)
    assert result.returncode == 1, result.stdout + result.stderr
    assert diagnostic in result.stdout
    assert "consumer.py:" in result.stdout


@pytest.mark.parametrize(
    ("pattern", "replacement", "diagnostic"),
    [
        (
            r"_PARAM_TYPES: dict\[str, dict\[str, _typing.Any\]\]",
            "_PARAM_TYPES",
            "[index]",
        ),
        (r"_typing.cast\(_Return_label,", '_typing.cast("str",', "[valid-type]"),
        (r"return _typing.cast\([^\n]+, _result\)", "return _result", "[no-any-return]"),
    ],
)
def test_control_missing_generated_types_fail_strict_mypy(
    tmp_path, source, mypy_cache, pattern, replacement, diagnostic
):
    mutated, count = re.subn(pattern, replacement, source)
    assert count > 0
    result = _check(tmp_path, mutated, cache=mypy_cache)
    assert result.returncode == 1, result.stdout + result.stderr
    assert diagnostic in result.stdout, result.stdout
    assert "orders_client.py:" in result.stdout


def test_an_empty_generated_client_passes_strict_mypy(tmp_path, mypy_cache):
    empty = Description.from_dict(
        {
            "service": "orders",
            "version": "1",
            "description_hash": "sha256:empty",
            "methods": [],
        }
    )
    result = _check(
        tmp_path, emit(empty), "from orders_client import OrdersClient\n", cache=mypy_cache
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("changed", ["client", "consumer"])
def test_cached_dependencies_do_not_hide_changes_to_orders_code(
    tmp_path, source, mypy_cache, monkeypatch, changed
):
    write_text = Path.write_text

    def write_with_fixed_timestamp(path, *args, **kwargs):
        result = write_text(path, *args, **kwargs)
        if path.name in {"orders_client.py", "consumer.py"}:
            os.utime(path, (1_700_000_000, 1_700_000_000))
        return result

    monkeypatch.setattr(Path, "write_text", write_with_fixed_timestamp)
    if changed == "client":
        before = "_typing.cast(_Return_label,"
        after = '_typing.cast("str"        ,'
        assert before in source
        broken_source, broken_consumer = source.replace(before, after), CONSUMER
        diagnostic, filename = "[valid-type]", "orders_client.py:"
    else:
        before, after = "label(str=42)", "label(str='')"
        assert before in CONSUMER
        broken_source, broken_consumer = source, CONSUMER.replace(before, after)
        diagnostic, filename = "[arg-type]", "consumer.py:"
    assert len(broken_source.encode()) == len(source.encode())
    assert len(broken_consumer.encode()) == len(CONSUMER.encode())

    for client, consumer, expected in [
        (source, CONSUMER, 0),
        (broken_source, broken_consumer, 1),
        (source, CONSUMER, 0),
    ]:
        result = _check(tmp_path, client, consumer, cache=mypy_cache)
        assert result.returncode == expected, result.stdout + result.stderr
        if expected:
            assert diagnostic in result.stdout, result.stdout
            assert filename in result.stdout, result.stdout


def test_orders_checks_do_not_write_to_an_inherited_cache(
    tmp_path, source, mypy_cache, monkeypatch
):
    inherited_cache = tmp_path / "unrelated-cache"
    monkeypatch.setenv("MYPY_CACHE_DIR", str(inherited_cache))

    result = _check(tmp_path, source, cache=mypy_cache)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not inherited_cache.exists(), "the check wrote into another invocation's cache"

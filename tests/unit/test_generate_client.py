"""Tests verifying client code generator emitter behavior, formatting, and determinism."""

import ast
import enum
import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from cliffracer.generate_client.emitter import (
    LINE_LENGTH,
    CannotEmit,
    annotation_text,
    emit,
)
from cliffracer.introspect import Description

REPO = Path(__file__).resolve().parents[2]
FIXTURE_DESCRIPTION = REPO / "tests" / "fixtures" / "typed_client" / "description.json"

DESC = {
    "service": "orders",
    "version": "1.4.0",
    "description_hash": "sha256:abc",
    "methods": [
        {
            "name": "create",
            "doc": "Create an order.",
            "signature_hash": "sha256:s1",
            "params": [
                {
                    "name": "order",
                    "type": {
                        "kind": "model",
                        "module": "tests.unit.test_generate_client",
                        "qualname": "Order",
                    },
                },
                {"name": "note", "type": {"kind": "scalar", "name": "str"}, "default": ""},
            ],
            "returns": {
                "kind": "model",
                "module": "tests.unit.test_generate_client",
                "qualname": "Receipt",
            },
        },
        {
            "name": "tags",
            "doc": None,
            "signature_hash": "sha256:s2",
            "params": [
                {
                    "name": "skus",
                    "type": {"kind": "list", "item": {"kind": "scalar", "name": "str"}},
                }
            ],
            "returns": {
                "kind": "dict",
                "value": {"kind": "optional", "inner": {"kind": "literal", "values": ["a", "b"]}},
            },
        },
    ],
}


class Order(BaseModel):
    sku: str


class Receipt(BaseModel):
    order_id: str


def _load(src: str, tmp_path, name: str = "orders_client"):
    path = tmp_path / f"{name}.py"
    path.write_text(src)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod, path


@pytest.mark.unit
def test_emit_is_deterministic():
    out1 = emit(Description.from_dict(DESC))
    out2 = emit(Description.from_dict(DESC))
    assert len(out1) > 100
    assert "class OrdersClient(ServiceClient):" in out1
    assert out1 == out2


@pytest.mark.unit
def test_annotation_text_for_every_kind():
    assert annotation_text({"kind": "scalar", "name": "none"}) == "None"
    assert (
        annotation_text({"kind": "list", "item": {"kind": "scalar", "name": "int"}}) == "list[int]"
    )
    assert (
        annotation_text({"kind": "optional", "inner": {"kind": "scalar", "name": "str"}})
        == "str | None"
    )
    assert annotation_text({"kind": "literal", "values": ["a", 1]}) == 'Literal["a", 1]'
    assert annotation_text({"kind": "model", "module": "m.n", "qualname": "X.Y"}) == "NX.Y"


@pytest.mark.unit
def test_the_generated_module_imports_and_has_typed_methods(tmp_path):
    """The annotations are the real classes, imported from where DESC says.

    Compared against `tests.unit.test_generate_client.Order` rather than the
    `Order` in this module's namespace, and they are NOT the same object:
    pytest imports this file as `test_generate_client` (there is no
    `tests/unit/__init__.py`), so importing it by its dotted path -- which is
    what the generated file does, and what a real Description would name --
    creates a second module object with its own classes. Asserting against the
    local name would fail for a reason that has nothing to do with the
    emitter; asserting only on `__qualname__` would pass even if the generated
    import resolved somewhere else entirely.
    """
    from tests.unit import test_generate_client as by_dotted_path

    mod, _ = _load(emit(Description.from_dict(DESC)), tmp_path)

    sig = inspect.signature(mod.OrdersClient.create)
    assert list(sig.parameters) == ["self", "order", "note"]
    assert sig.parameters["order"].annotation is by_dotted_path.Order
    assert sig.return_annotation is by_dotted_path.Receipt
    assert mod.OrdersClient.SIGNATURES == {"create": "sha256:s1", "tags": "sha256:s2"}
    assert mod.OrdersClient.DESCRIPTION_HASH == "sha256:abc"


@pytest.mark.unit
def test_the_generated_file_is_already_formatted_for_ruffs_defaults(tmp_path):
    """Otherwise every generated client is a diff the first time anyone runs
    the formatter, and the file that is supposed to be checked in and forgotten
    becomes one more thing to reformat.

    THE DEFAULT CONFIGURATION IS THE TARGET, and it has to be one target rather
    than two. Ruff resolves its settings from the current directory when the
    file has no config above it, and formatting for two line lengths at once is
    not possible: `DESCRIPTION_HASH = "sha256:..."` is 92 characters, so ruff
    at 88 wraps it in parentheses and ruff at 100 -- this repository's setting
    -- unwraps it again. A generated file lands in a tree whose settings nobody
    here controls, so it targets the narrowest thing it is likely to meet.
    Formatting is a preference; lint below is not, and that IS checked both
    ways.
    """
    _, path = _load(emit(Description.from_dict(DESC)), tmp_path, name="fmt_client")

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # Verify E501 line length compliance across common limits.
    for limit in (88, 100, 120):
        long_lines = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--select",
                "E501",
                "--line-length",
                str(limit),
                str(path),
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert long_lines.returncode == 0, long_lines.stdout + long_lines.stderr


@pytest.mark.unit
@pytest.mark.parametrize("where", ["repo", "elsewhere"])
def test_the_generated_file_passes_lint_under_either_configuration(tmp_path, where):
    """Verify generated client passes linter checks regardless of working directory configuration."""
    _, path = _load(emit(Description.from_dict(DESC)), tmp_path, name="lint_client")
    cwd = str(REPO) if where == "repo" else str(tmp_path)

    lint = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(path)],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    assert lint.returncode == 0, lint.stdout + lint.stderr


@pytest.mark.unit
@pytest.mark.parametrize("source", ["hand-written", "captured"])
def test_no_emitted_line_is_longer_than_the_target(source):
    """Verify emitted code contains no line exceeding the target length limit."""
    desc = Description.from_dict(
        DESC if source == "hand-written" else json.loads(FIXTURE_DESCRIPTION.read_text())
    )

    too_long = [
        (number, len(line), line)
        for number, line in enumerate(emit(desc).splitlines(), 1)
        if len(line) > LINE_LENGTH
    ]

    assert not too_long, too_long


@pytest.mark.unit
def test_a_private_model_module_cannot_be_emitted():
    bad = {
        **DESC,
        "methods": [
            {
                **DESC["methods"][0],
                "returns": {"kind": "model", "module": "__main__", "qualname": "X"},
            }
        ],
    }
    with pytest.raises(CannotEmit, match="__main__"):
        emit(Description.from_dict(bad))


@pytest.mark.unit
def test_no_timestamps_or_environment_in_the_output():
    src = emit(Description.from_dict(DESC))
    assert "GENERATED_AT" not in src and "202" not in src.split("\n", 3)[0]


# --- the committed fixture description, and the emitter over a REAL one -------


@pytest.mark.unit
def test_the_committed_description_matches_the_class():
    """The fixture cannot drift from the service it describes.

    `tests/fixtures/typed_client/description.json` is what the tests below read,
    so it has to be what `describe()` produces today. Change a handler in the
    fixture service and this fails until the capture script is re-run -- which
    is the point: a stale fixture would let the emitter tests pass over a
    service that no longer exists.
    """
    from tests.fixtures.typed_client.capture_description import current

    assert FIXTURE_DESCRIPTION.read_text() == current() + "\n"


@pytest.mark.unit
def test_a_real_description_emits_a_formatted_importable_client(tmp_path):
    """The hand-written DESC above covers the type kinds; this covers a
    description nobody wrote by hand -- nested models, a list return, an
    optional return, a literal with a default -- straight from a real class."""
    desc = Description.from_dict(json.loads(FIXTURE_DESCRIPTION.read_text()))

    mod, path = _load(emit(desc), tmp_path, name="warehouse_client_fixture")

    from tests.fixtures.typed_client.models import Line, Receipt

    assert mod.WarehouseE2eClient.SERVICE == "warehouse_e2e"
    assert set(mod.WarehouseE2eClient.SIGNATURES) == {"create", "fail", "find", "lines"}

    # The three shapes the hand-written DESC does not have between them.
    assert inspect.signature(mod.WarehouseE2eClient.find).return_annotation == Receipt | None
    assert inspect.signature(mod.WarehouseE2eClient.lines).return_annotation == list[Line]
    create = inspect.signature(mod.WarehouseE2eClient.create)
    assert create.parameters["note"].default == ""

    for cmd, cwd in ((["format", "--check"], tmp_path), (["check"], REPO), (["check"], tmp_path)):
        result = subprocess.run(
            [sys.executable, "-m", "ruff", *cmd, str(path)],
            capture_output=True,
            text=True,
            cwd=str(cwd),
        )
        assert result.returncode == 0, result.stdout + result.stderr


class _FixtureNum(int, enum.Enum):
    ONE = 1
    TWO = 2


@pytest.mark.unit
def test_service_version_cannot_escape_docstring(tmp_path):
    # Verify version with triple quotes does not execute code on import.
    evil_version = 'sha256:x"""\nMARKER = "escaped"\n"""'
    desc_dict = dict(DESC)
    desc_dict["version"] = evil_version
    source = emit(Description.from_dict(desc_dict))

    # Verify AST structure: only valid module statements, no MARKER assignment
    tree = ast.parse(source)
    top_level_assigns = [
        t.id
        for stmt in tree.body
        if isinstance(stmt, ast.Assign)
        for t in stmt.targets
        if hasattr(t, "id")
    ]
    assert "MARKER" not in top_level_assigns
    assert "SIGNATURES" in top_level_assigns

    # Verify module loads and MARKER is not an attribute
    mod, _ = _load(source, tmp_path, name="evil_doc_client")
    assert not hasattr(mod, "MARKER")
    assert mod.OrdersClient.VERSION == evil_version


@pytest.mark.unit
@pytest.mark.parametrize("bad_service", ["order.service", "123", "order service", "", "service"])
def test_invalid_service_names_are_refused(bad_service):
    # Refuse service names that cannot form valid class identifiers.
    desc_dict = dict(DESC)
    desc_dict["service"] = bad_service
    with pytest.raises(CannotEmit) as exc_info:
        emit(Description.from_dict(desc_dict))
    assert "cannot generate a client for" in str(exc_info.value)


@pytest.mark.unit
def test_annotation_text_handles_empty_and_enum_literals():
    # Verify emitter handles empty and enum literal values.
    assert annotation_text({"kind": "literal", "values": []}) == "Literal[()]"
    assert (
        annotation_text({"kind": "literal", "values": [_FixtureNum.ONE, _FixtureNum.TWO]})
        == "Literal[1, 2]"
    )


@pytest.mark.unit
def test_keyword_only_parameter_ordering_emits_valid_ast():
    """Verify non-default parameter following default parameter emits keyword-only asterisk."""
    from cliffracer.introspect import Method, Param

    desc = Description(
        service="kw-service",
        version="1.0.0",
        methods=[
            Method(
                name="handler",
                doc="Test handler",
                params=[
                    Param(
                        name="a",
                        type={"kind": "scalar", "name": "int"},
                        has_default=True,
                        default=1,
                    ),
                    Param(name="b", type={"kind": "scalar", "name": "int"}, has_default=False),
                ],
                returns={"kind": "scalar", "name": "int"},
                signature_hash="sig",
            )
        ],
        description_hash="hash",
    )
    code = emit(desc)
    assert "async def handler(self, a: int = 1, *, b: int) -> int:" in code
    ast.parse(code)


@pytest.mark.unit
@pytest.mark.parametrize(
    "method_name", ["verify", "close", "SERVICE", "VERSION", "DESCRIPTION_HASH", "SIGNATURES"]
)
def test_reserved_method_names_refused_by_emitter(method_name):
    """Verify methods named after ServiceClient members are refused by emitter."""
    from cliffracer.introspect import Method

    desc = Description(
        service="bad-methods",
        version="1.0.0",
        methods=[
            Method(
                name=method_name,
                doc=None,
                params=[],
                returns={"kind": "scalar", "name": "none"},
                signature_hash="sig",
            )
        ],
        description_hash="hash",
    )
    with pytest.raises(CannotEmit) as exc:
        emit(desc)
    assert method_name in str(exc.value)


@pytest.mark.unit
def test_leading_underscore_param_refused_by_emitter():
    """Verify parameters with leading underscore are refused by emitter."""
    from cliffracer.introspect import Method, Param

    desc = Description(
        service="underscore-param",
        version="1.0.0",
        methods=[
            Method(
                name="query",
                doc=None,
                params=[Param(name="_count", type={"kind": "scalar", "name": "int"})],
                returns={"kind": "scalar", "name": "none"},
                signature_hash="sig",
            )
        ],
        description_hash="hash",
    )
    with pytest.raises(CannotEmit) as exc:
        emit(desc)
    assert "query._count" in str(exc.value)

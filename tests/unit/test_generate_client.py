"""Tests verifying client code generator emitter behavior, formatting, and determinism."""

import ast
import enum
import importlib.util
import inspect
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Literal as TLiteral

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer.generate_client.emitter import (
    LINE_LENGTH,
    CannotEmit,
    annotation_text,
    emit,
)
from cliffracer.introspect import Description

pytestmark = pytest.mark.unit

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
                # The inputs that made the formatting guard below blind: a
                # double quote sends ruff to single quotes, and a non-BMP
                # character is what `json.dumps` escaped as a surrogate pair.
                {
                    "name": "quoted",
                    "type": {"kind": "scalar", "name": "str"},
                    "default": 'he said "no"',
                },
                {
                    "name": "emoji",
                    "type": {"kind": "scalar", "name": "str"},
                    "default": "ok \U0001f642",
                },
                # A mutable container default: what the formatting guard and the
                # lint guard were both blind to. `repr` spelled its strings with
                # single quotes, and `flake8-bugbear`'s B006 -- which this
                # repository selects -- fires on the default itself.
                {
                    "name": "tags",
                    "type": {"kind": "list", "item": {"kind": "scalar", "name": "str"}},
                    "default": ["a", "b"],
                },
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


def test_emit_is_deterministic():
    out1 = emit(Description.from_dict(DESC))
    out2 = emit(Description.from_dict(DESC))
    assert len(out1) > 100
    assert "class OrdersClient(ServiceClient):" in out1
    assert out1 == out2


_EMIT_IN_A_FRESH_PROCESS = (
    "import json, sys\n"
    "from cliffracer.generate_client.emitter import emit\n"
    "from cliffracer.introspect import Description\n"
    "sys.stdout.write(emit(Description.from_dict(json.load(sys.stdin))))\n"
)


def _emitted_elsewhere(tmp_path, name: str, hash_seed: str) -> str:
    """`emit` run in its own interpreter, with its own working directory, hash seed and hostname."""
    cwd = tmp_path / name
    cwd.mkdir()
    env = {
        **os.environ,
        "PYTHONHASHSEED": hash_seed,
        "HOSTNAME": f"host-{name}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    done = subprocess.run(
        [sys.executable, "-c", _EMIT_IN_A_FRESH_PROCESS],
        input=json.dumps(DESC),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_emit_gives_the_same_bytes_in_another_process_directory_and_hash_seed(tmp_path):
    """Two calls in one interpreter agree with anything stable for the life of the process: a
    hostname, a working directory, a set order fixed by the hash seed. The emitter's claim is
    that the same Description gives identical bytes across environments, which is what makes a
    generated client committable, so this runs it in two."""
    one = _emitted_elsewhere(tmp_path, "one", "1")
    two = _emitted_elsewhere(tmp_path, "two", "2")

    assert one == two
    assert one == emit(Description.from_dict(DESC))


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
    # `MNX.Y`, not `NX.Y`: the head `X` is what an import binds, `.Y` is the
    # attribute on it, and the prefix is the one `emit` itself would build for a
    # colliding name -- all module parts, not just the last. The old expectation
    # was a name no emitted import ever bound.
    assert annotation_text({"kind": "model", "module": "m.n", "qualname": "X.Y"}) == "MNX.Y"
    assert annotation_text({"kind": "model", "module": "m.n", "qualname": "X"}) == "MNX"
    # A dict's keys are always `str`: the description carries only the value type.
    assert (
        annotation_text({"kind": "dict", "value": {"kind": "scalar", "name": "int"}})
        == "dict[str, int]"
    )
    assert (
        annotation_text(
            {"kind": "dict", "value": {"kind": "list", "item": {"kind": "scalar", "name": "str"}}}
        )
        == "dict[str, list[str]]"
    )


def test_an_unknown_kind_and_an_unknown_scalar_are_refused_by_name():
    with pytest.raises(CannotEmit, match="unknown TypeRef kind 'tuple'"):
        annotation_text({"kind": "tuple"})
    with pytest.raises(CannotEmit, match="unknown scalar type 'decimal'"):
        annotation_text({"kind": "scalar", "name": "decimal"})


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
    # `quoted` and `emoji` are in DESC so the formatting guard below sees a
    # string default that sends ruff to single quotes and one that `json.dumps`
    # would have escaped as a surrogate pair.
    assert list(sig.parameters) == ["self", "order", "note", "quoted", "emoji", "tags"]
    assert sig.parameters["order"].annotation is by_dotted_path.Order
    assert sig.return_annotation is by_dotted_path.Receipt
    assert mod.OrdersClient.SIGNATURES == {"create": "sha256:s1", "tags": "sha256:s2"}
    assert mod.OrdersClient.DESCRIPTION_HASH == "sha256:abc"


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


def _return_alias_forms(src: str) -> dict[str, str]:
    """Each module-level `_Return_*` binding, by the statement form that binds it."""
    forms = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.TypeAlias) and node.name.id.startswith("_Return_"):
            forms[node.name.id] = "type"
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.startswith("_Return_")
        ):
            forms[node.target.id] = "annotated"
    return forms


def test_each_return_alias_is_a_type_statement():
    src = emit(Description.from_dict(DESC))

    assert _return_alias_forms(src) == {"_Return_create": "type", "_Return_tags": "type"}
    assert "UP040" not in src


def _up040(path: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "UP040",
            "--target-version",
            "py312",
            str(path),
        ],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def test_the_generated_file_needs_no_type_alias_suppression(tmp_path):
    """UP040 asks for `type` statements once the target is 3.12, the floor a
    cliffracer installation runs on. The file carries no suppression for it."""
    _, path = _load(emit(Description.from_dict(DESC)), tmp_path, name="up040_client")

    result = _up040(path, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_CONTROL_the_annotated_alias_form_trips_up040(tmp_path):
    """The rule is live in this invocation: the emitted file with its aliases
    written as `X: TypeAlias = ...` and no suppression comment fails it. Built
    from whatever form the emitter writes, so the control holds on either."""
    src = emit(Description.from_dict(DESC))
    annotated = re.sub(r"^type (_Return_\w+) = ", r"\1: _typing.TypeAlias = ", src, flags=re.M)
    annotated = re.sub(r"^# ruff: noqa.*\n", "", annotated, flags=re.M)
    assert _return_alias_forms(annotated) == {
        "_Return_create": "annotated",
        "_Return_tags": "annotated",
    }
    assert "noqa" not in annotated.split("import typing")[0]
    path = tmp_path / "annotated_client.py"
    path.write_text(annotated)

    result = _up040(path, tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.count("Type alias `_Return_") == 2, result.stdout


# One ordinary sentence. `DESC`'s longest doc is "Create an order." and the
# committed fixture's is 62 characters, so the guard below measured only
# docstrings that could not reach the limit however they were emitted.
LONG_DOC = (
    "Reconcile the warehouse ledger against the shipment manifest and return "
    "the identifier of the reconciliation run that was created."
)

LONG_DOC_DESC = {
    "service": "ledger",
    "version": "1",
    "description_hash": "sha256:d",
    "methods": [
        {
            "name": "reconcile",
            "signature_hash": "sha256:s",
            "doc": LONG_DOC + "\n\nThe identifier is stable across retries of one batch.",
            "params": [],
            "returns": {"kind": "scalar", "name": "str"},
        }
    ],
}


# Ruff's default line length, written here and not read from the emitter: the emitter decides
# where to wrap from `LINE_LENGTH`, so a threshold taken from it moves with the thing it checks.
RUFF_DEFAULT_LINE_LENGTH = 88


def test_the_emitters_target_is_ruffs_default():
    assert LINE_LENGTH == RUFF_DEFAULT_LINE_LENGTH


@pytest.mark.parametrize("source", ["hand-written", "captured", "long-docstring"])
def test_no_emitted_line_is_longer_than_the_target(source):
    """Verify emitted code contains no line exceeding ruff's default line length."""
    if source == "long-docstring":
        desc = Description.from_dict(LONG_DOC_DESC)
    else:
        desc = Description.from_dict(
            DESC if source == "hand-written" else json.loads(FIXTURE_DESCRIPTION.read_text())
        )

    too_long = [
        (number, len(line), line)
        for number, line in enumerate(emit(desc).splitlines(), 1)
        if len(line) > RUFF_DEFAULT_LINE_LENGTH
    ]

    assert not too_long, too_long


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


def test_no_generated_at_constant_in_the_output():
    """One spelling only. What the output may not carry (a time, a host, a user, a path, an
    order the process decides) is read in `test_generated_client_is_the_same_in_any_environment.py`."""
    assert "GENERATED_AT" not in emit(Description.from_dict(DESC))


# --- the committed fixture description, and the emitter over a REAL one -------


def test_the_committed_description_is_what_the_capture_script_writes(tmp_path, monkeypatch, capsys):
    """The fixture cannot drift from the service it describes, and the script that regenerates it
    is run, not re-implemented.

    `tests/fixtures/typed_client/description.json` is what the tests below read, so it has to be
    what the capture script writes today. This calls its `main()` with the target moved into a
    temporary directory and compares the bytes it wrote with the committed file: a wrong file
    name, a missing trailing newline or a change in what is written is found here, and not the
    next time someone regenerates the fixture and mistakes it for drift in the service.
    """
    from tests.fixtures.typed_client import capture_description

    assert capture_description.PATH == FIXTURE_DESCRIPTION, (
        "the script would regenerate a file other than the committed one"
    )
    written = tmp_path / "description.json"
    monkeypatch.setattr(capture_description, "PATH", written)

    capture_description.main()

    assert written.read_bytes() == FIXTURE_DESCRIPTION.read_bytes()
    assert str(written) in capsys.readouterr().out


def test_CONTROL_the_capture_script_writes_the_class_as_it_is_now(tmp_path, monkeypatch):
    """The comparison above is red for a class that has moved: change what is described and the
    script's output no longer equals the committed file."""
    from tests.fixtures.typed_client import capture_description

    written = tmp_path / "description.json"
    monkeypatch.setattr(capture_description, "PATH", written)
    monkeypatch.setattr(capture_description, "VERSION", "9.9.9")

    capture_description.main()

    assert written.read_bytes() != FIXTURE_DESCRIPTION.read_bytes()


def test_a_real_description_emits_a_formatted_importable_client(tmp_path):
    """The hand-written DESC above covers the type kinds; this covers a
    description nobody wrote by hand -- nested models, a list return, an
    optional return, a literal with a default -- straight from a real class."""
    desc = Description.from_dict(json.loads(FIXTURE_DESCRIPTION.read_text()))

    mod, path = _load(emit(desc), tmp_path, name="warehouse_client_fixture")

    from tests.fixtures.typed_client.models import Line, Receipt

    assert mod.WarehouseE2eClient.SERVICE == "warehouse_e2e"
    assert set(mod.WarehouseE2eClient.SIGNATURES) == {
        "create",
        "fail",
        "find",
        "lines",
        "route",
    }

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


@pytest.mark.parametrize("bad_service", ["order.service", "123", "order service", "", "service"])
def test_invalid_service_names_are_refused(bad_service):
    # Refuse service names that cannot form valid class identifiers.
    desc_dict = dict(DESC)
    desc_dict["service"] = bad_service
    with pytest.raises(CannotEmit) as exc_info:
        emit(Description.from_dict(desc_dict))
    assert "cannot generate a client for" in str(exc_info.value)


def test_annotation_text_handles_empty_and_enum_literals():
    # Verify emitter handles empty and enum literal values.
    assert annotation_text({"kind": "literal", "values": []}) == "Literal[()]"
    assert (
        annotation_text({"kind": "literal", "values": [_FixtureNum.ONE, _FixtureNum.TWO]})
        == "Literal[1, 2]"
    )


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


def _every_name_a_client_has() -> list[str]:
    """Class members, private and dunder ones too, and the attributes the constructor sets.

    Read off the class and a constructed client, so a member added to `ServiceClient` is covered
    without anyone editing a list. The emitted method bodies call `_call` and `_encode`, and a
    description from the wire is the one place a method could be named after one.
    """
    from cliffracer.client import ServiceClient

    client = ServiceClient(service="orders", verify=False)
    return sorted(set(dir(ServiceClient)) | set(vars(client)))


def test_the_surface_the_reserved_name_test_walks_is_not_empty_and_holds_the_calls_a_body_makes():
    names = _every_name_a_client_has()

    assert {"verify", "close", "SIGNATURES", "_call", "_encode", "_subject"} <= set(names)


@pytest.mark.parametrize("method_name", _every_name_a_client_has())
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


def test_CONTROL_a_method_that_is_no_client_member_is_emitted():
    """The refusal is not a refusal of everything: an ordinary name still produces a client."""
    from cliffracer.introspect import Method

    desc = Description(
        service="fine",
        version="1.0.0",
        methods=[
            Method(
                name="place",
                doc=None,
                params=[],
                returns={"kind": "scalar", "name": "none"},
                signature_hash="sig",
            )
        ],
        description_hash="hash",
    )

    assert "async def place(self)" in emit(desc)


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


# --- a default the language has no literal for --------------------------------
#
# `_literal` sends everything that is not a string or an enum through `repr`,
# and `repr` is a round trip for every JSON-shaped value except a non-finite
# float: it spells them `inf`, `-inf` and `nan`, which are NAMES. The generated
# module raised `NameError` at import, and the command still exited 0 and left
# the file behind -- against `cli.py`'s rule that no failure writes a file.
#
# These assert the module IMPORTS and that the value read back is the value that
# went in. A test on the emitted text would have passed on `inf`.
#
# A current service cannot publish such a default: `describe` refuses it (the test below that
# names the parameter), because JSON has no literal for it. The emitter still reads one, from
# a description that was written by a service that predates the refusal and from one built by
# hand, and those are the only paths that reach it. The tests here build that description: a
# described service with a finite default of the same annotation, whose default is then replaced
# by the non-finite value the older description carried.


def _description_with_default(default):
    return {
        "service": "caps",
        "version": "1",
        "description_hash": "sha256:d",
        "methods": [
            {
                "name": "limit",
                "doc": None,
                "signature_hash": "sha256:s",
                "params": [
                    {
                        "name": "cap",
                        "type": {"kind": "scalar", "name": "float"},
                        "default": default,
                    }
                ],
                "returns": {"kind": "scalar", "name": "str"},
            }
        ],
    }


@pytest.mark.parametrize(
    ("label", "default"),
    [("inf", float("inf")), ("-inf", float("-inf")), ("nan", float("nan"))],
)
def test_a_non_finite_default_produces_a_module_that_imports(label, default, tmp_path):
    """The defect, asserted where it bites: at import, not in the text."""
    src = emit(Description.from_dict(_description_with_default(default)))

    mod, _ = _load(src, tmp_path, name=f"nonfinite_{label.strip('-')}_client")

    read_back = inspect.signature(mod.CapsClient.limit).parameters["cap"].default
    assert isinstance(read_back, float), read_back
    if math.isnan(default):
        assert math.isnan(read_back), read_back
    else:
        assert read_back == default, read_back


@pytest.mark.parametrize(
    ("label", "default"), [("plain", 1.5), ("zero", 0.0), ("negative_zero", -0.0)]
)
def test_CONTROL_an_ordinary_float_default_is_emitted_as_a_literal(label, default, tmp_path):
    """The values that were already right must stay right, and must stay
    literals: wrapping every float in `float("...")` would satisfy the tests
    above while making the common case unreadable."""
    src = emit(Description.from_dict(_description_with_default(default)))

    assert 'float("' not in src, src
    mod, _ = _load(src, tmp_path, name=f"finite_{label}_client")

    read_back = inspect.signature(mod.CapsClient.limit).parameters["cap"].default
    assert read_back == default
    assert math.copysign(1, read_back) == math.copysign(1, default), "the sign of zero moved"


def test_CONTROL_a_non_finite_default_is_still_formatted_for_ruffs_defaults(tmp_path):
    """The formatting guard above uses a fixture with no non-finite default, so
    it cannot speak for this spelling. `float("inf")` is an expression where
    every other default is a literal, and an expression is where a formatter is
    most likely to want different parentheses or quotes."""
    src = emit(Description.from_dict(_description_with_default(float("inf"))))
    _, path = _load(src, tmp_path, name="nonfinite_fmt_client")

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_describe_refuses_a_non_finite_default_and_names_the_parameter():
    """The premise the tests above rest on, as it stands now: no current service produces one.

    The refusal is by name, so the operator reading it knows which parameter of which handler to
    change; a refusal that only said "not finite" would pass a test that only asked for a raise.
    """
    from cliffracer import CliffracerService, rpc
    from cliffracer.core.typed_rpc import UntypedHandler
    from cliffracer.introspect import describe

    class Uncapped(CliffracerService):
        @rpc
        async def limit(self, cap: float = float("inf")) -> str: ...

    with pytest.raises(UntypedHandler) as refused:
        describe(Uncapped, service="caps", version="1")

    message = str(refused.value)
    assert "Uncapped.limit" in message and "the default of parameter 'cap'" in message, message
    assert "float | None = None" in message, message


# --- the same value one level down -------------------------------------------
#
# `_literal` handed a container to `repr`, which recurses with its OWN rules, so
# `list[float] = [float("inf")]` emitted `[inf]` -- the same NameError one level
# down -- and `list[str] = ["a"]` emitted `['a']`, which ruff reformats.
#
# A PARAMETERISED container is a supported annotation and reaches here intact.
# It is the BARE `list` that `describe` refuses, as `UntypedHandler`, and an
# enumeration that drove the bare form cannot speak for the parameterised one.
# That mistake is why every case below states its annotation rather than
# deriving it from its default.
#
# ONE SERVICE PER SHAPE, each emitted and imported on its own. A single service
# carrying all ten defaults shares one generated module, so any one broken
# default stops that module importing and every case ERRORS together -- which is
# how the first version of this file reported a dict-only mutation as twelve
# indiscriminate errors, losing exactly the discrimination the parametrisation
# is for.
#
# B006 is enabled here and these defaults are mutable containers on purpose: a
# container default is the input these fixtures exist to emit, and the linter's
# advice ("replace with None") would delete the test. Marked per line, so an
# accidental one elsewhere still reds.


# The first six carry the ANNOTATION of a non-finite shape with a finite default, because
# `describe` refuses the non-finite one. `_emit_and_load` puts the value the shape is named for
# into the described default (NON_FINITE_SHAPES), which is what an older service's description
# carries.


class ScalarInf(CliffracerService):
    @rpc
    async def m(self, cap: float = 0.0) -> str: ...


class ListInf(CliffracerService):
    @rpc
    async def m(self, cap: list[float] = [0.0]) -> str: ...  # noqa: B006


class DictInf(CliffracerService):
    @rpc
    async def m(self, cap: dict[str, float] = {"a": 0.0}) -> str: ...  # noqa: B006


class ListNan(CliffracerService):
    @rpc
    async def m(self, cap: list[float] = [0.0]) -> str: ...  # noqa: B006


class NestedListInf(CliffracerService):
    @rpc
    async def m(self, cap: list[list[float]] = [[0.0]]) -> str: ...  # noqa: B006


class DictOfListsInf(CliffracerService):
    @rpc
    async def m(
        self,
        cap: dict[str, list[float]] = {"a": [0.0]},  # noqa: B006
    ) -> str: ...


class FiniteList(CliffracerService):
    @rpc
    async def m(self, cap: list[float] = [1.5]) -> str: ...  # noqa: B006


class StringList(CliffracerService):
    @rpc
    async def m(self, cap: list[str] = ["a", "b"]) -> str: ...  # noqa: B006


class EmptyList(CliffracerService):
    @rpc
    async def m(self, cap: list[float] = []) -> str: ...  # noqa: B006


class StringDict(CliffracerService):
    @rpc
    async def m(self, cap: dict[str, str] = {"k": "v"}) -> str: ...  # noqa: B006


class LongScalars(CliffracerService):
    """Over the line limit with NO container default: the length bound alone.

    Every other shape here either fits flat or carries a container default, and
    a container default short-circuits the explode condition before the length
    bound is consulted. So this is the only shape whose signature the length
    bound decides, and the only one that can observe it. Flat form measures 139
    characters against a limit of 88.

    Added because removing the length bound from `_signature` reded nothing:
    widening `DESC` to carry a container default took that conjunct's last
    witness with it, since `DESC.create` is now suppressed and short-circuits.
    """

    @rpc
    async def m(
        self,
        cap: str = "x",
        warehouse_identifier: str = "w",
        shipment_reference: str = "s",
        correlation_token: str = "c",
    ) -> str: ...


SHAPES = [
    ("scalar_inf", ScalarInf, float("inf")),
    ("list_inf", ListInf, [float("inf")]),
    ("dict_inf", DictInf, {"a": float("inf")}),
    ("list_nan", ListNan, [float("nan")]),
    ("nested_list_inf", NestedListInf, [[float("inf")]]),
    ("dict_of_lists_inf", DictOfListsInf, {"a": [float("inf")]}),
    ("finite_list", FiniteList, [1.5]),
    ("string_list", StringList, ["a", "b"]),
    ("empty_list", EmptyList, []),
    ("string_dict", StringDict, {"k": "v"}),
    ("long_scalars", LongScalars, "x"),
]


def _same(read_back, expected):
    """Structural equality that treats nan as equal to nan.

    `float("nan") != float("nan")`, so a plain `==` on a container holding one
    is false however right the value is.
    """
    if isinstance(expected, float) and math.isnan(expected):
        return isinstance(read_back, float) and math.isnan(read_back)
    if isinstance(expected, list):
        return (
            isinstance(read_back, list)
            and len(read_back) == len(expected)
            and all(_same(r, e) for r, e in zip(read_back, expected, strict=True))
        )
    if isinstance(expected, dict):
        return (
            isinstance(read_back, dict)
            and read_back.keys() == expected.keys()
            and all(_same(read_back[k], expected[k]) for k in expected)
        )
    return read_back == expected


NON_FINITE_SHAPES = {
    "scalar_inf",
    "list_inf",
    "dict_inf",
    "list_nan",
    "nested_list_inf",
    "dict_of_lists_inf",
}


def _emit_and_load(cls, name, tmp_path):
    """The chain: the decorated class, `describe`, `emit`, and an import.

    A shape in NON_FINITE_SHAPES has its described default replaced by the non-finite value it is
    named for, since a current `describe` refuses to produce one.
    """
    from cliffracer.introspect import describe

    described = describe(cls, service="caps", version="1")
    if name in NON_FINITE_SHAPES:
        wire = described.to_dict()
        wire["methods"][0]["params"][0]["default"] = next(e for n, _, e in SHAPES if n == name)
        described = Description.from_dict(wire)
    src = emit(described)
    mod, path = _load(src, tmp_path, name=f"shape_{name}_client")
    return mod, path, src


@pytest.mark.parametrize(("name", "cls", "expected"), SHAPES, ids=[s[0] for s in SHAPES])
def test_every_default_shape_reads_back_as_itself(name, cls, expected, tmp_path):
    """The module imports and the value is the value that went in.

    Read off `inspect.signature` rather than off the emitted text, because the
    text is what looked right while the module would not load.
    """
    mod, _, _ = _emit_and_load(cls, name, tmp_path)

    read_back = inspect.signature(mod.CapsClient.m).parameters["cap"].default

    assert _same(read_back, expected), f"{read_back!r} is not {expected!r}"


@pytest.mark.parametrize(("name", "cls", "expected"), SHAPES, ids=[s[0] for s in SHAPES])
def test_CONTROL_every_default_shape_is_formatted_for_ruffs_defaults(name, cls, expected, tmp_path):
    """`repr` spells a container's strings with single quotes, and `float("inf")`
    is an expression where every other default is a literal. Either can be valid
    Python that the formatter still wants to rewrite, so every shape is checked
    rather than the one the fixture above happens to use."""
    _, path, _ = _emit_and_load(cls, name, tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("name", "cls", "spelling"),
    [
        ("finite_list", FiniteList, "cap: list[float] = [1.5]"),
        ("string_list", StringList, 'cap: list[str] = ["a", "b"]'),
        ("empty_list", EmptyList, "cap: list[float] = []"),
        ("string_dict", StringDict, 'cap: dict[str, str] = {"k": "v"}'),
    ],
)
def test_CONTROL_an_ordinary_container_is_still_a_plain_literal(name, cls, spelling, tmp_path):
    """Wrapping every float would satisfy the read-back tests while making the
    ordinary case unreadable, and `repr`-ing a whole container would satisfy the
    finite cases while leaving the non-finite ones broken."""
    _, _, src = _emit_and_load(cls, name, tmp_path)

    assert spelling in src, src


# --- a string is spelled the way ruff spells it, and reads back -------------
#
# `_literal` emitted every string through `json.dumps`, which is wrong twice.
#
# The quote style: ruff prefers double and switches to single only when that
# STRICTLY reduces escapes, so `he said "no"` becomes `'he said "no"'` where
# `json.dumps` gives `"he said \"no\""` and ruff reformats it. The module
# docstring claims the output is formatted by construction, and
# `test_the_generated_file_is_already_formatted_for_ruffs_defaults` asserts
# that -- and passed, because its fixture had no such string. That fixture now
# has one.
#
# The escaping: `json.dumps` spells a non-BMP character as a UTF-16 surrogate
# pair. Valid JSON; as Python, two lone surrogates. So an emoji in a default
# came back out of the generated client as a DIFFERENT STRING, which is worse
# than a formatting diff and was not in the report.
#
# Each case is asserted three ways, because the three can disagree: the
# spelling ruff leaves alone, the value it reads back as, and pure ASCII so the
# generated file needs no encoding declaration.

AWKWARD_STRINGS = [
    ("plain", "plain"),
    ("a double quote", 'he said "no"'),
    ("an apostrophe", "it's fine"),
    ("both", 'he said "no" and it\'s fine'),
    ("one of each, a tie", "one \" and one '"),
    ("only a double quote", '"'),
    ("only an apostrophe", "'"),
    ("two doubles and an apostrophe", '""\''),
    ("a backslash", "back\\slash"),
    ("a backslash after a quote", '"\\'),
    ("a quote after a backslash", '\\"'),
    ("a newline", "new\nline"),
    ("a tab", "tab\there"),
    ("a carriage return", "\r\n"),
    ("a null", "\x00null"),
    ("a latin-1 character", "caf\u00e9"),
    ("a line separator", "a\u2028b"),
    ("a non-BMP character", "emoji \U0001f642"),
    ("a non-BMP character and a quote", 'emoji \U0001f642 and "q"'),
    ("empty", ""),
    ("a space", " "),
    ("two apostrophes", "''"),
    ("two double quotes", '""'),
]


def _with_string_default(default: str) -> dict:
    return {
        "service": "notes",
        "version": "1",
        "description_hash": "sha256:d",
        "methods": [
            {
                "name": "note",
                "doc": None,
                "signature_hash": "sha256:s",
                "params": [
                    {
                        "name": "text",
                        "type": {"kind": "scalar", "name": "str"},
                        "default": default,
                    }
                ],
                "returns": {"kind": "scalar", "name": "str"},
            }
        ],
    }


@pytest.mark.parametrize(("label", "value"), AWKWARD_STRINGS, ids=[s[0] for s in AWKWARD_STRINGS])
def test_a_string_default_reads_back_as_itself(label, value, tmp_path):
    """The value, not the spelling. A surrogate pair looks right and is not."""
    src = emit(Description.from_dict(_with_string_default(value)))

    mod, _ = _load(src, tmp_path, name=f"strings_{abs(hash(label))}_client")

    read_back = inspect.signature(mod.NotesClient.note).parameters["text"].default
    assert read_back == value, f"{read_back!r} is not {value!r}"


@pytest.mark.parametrize(("label", "value"), AWKWARD_STRINGS, ids=[s[0] for s in AWKWARD_STRINGS])
def test_a_string_default_is_spelled_the_way_ruff_spells_it(label, value, tmp_path):
    """`ruff format --check` on the generated file, per string.

    The guard above uses one fixture; this asks the question of every awkward
    string, which is what the guard could not do.
    """
    src = emit(Description.from_dict(_with_string_default(value)))
    _, path = _load(src, tmp_path, name=f"fmt_{abs(hash(label))}_client")

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(("label", "value"), AWKWARD_STRINGS, ids=[s[0] for s in AWKWARD_STRINGS])
def test_a_string_default_is_emitted_as_pure_ascii(label, value, tmp_path):
    """A generated file with a literal non-ASCII character is fine in Python 3
    and depends on the writer's encoding; escaping keeps that out of it."""
    from cliffracer.generate_client.emitter import _string_literal

    assert all(ord(c) < 128 for c in _string_literal(value)), _string_literal(value)


def test_CONTROL_a_tie_stays_double_quoted():
    """The case a plausible rule gets wrong.

    "contains a double quote, so switch to single" is nearly right: with one of
    each, switching saves no escapes and ruff keeps the double-quoted spelling.
    """
    from cliffracer.generate_client.emitter import _string_literal

    assert _string_literal("one \" and one '") == '"one \\" and one \'"'


def test_CONTROL_a_string_with_no_quotes_is_double_quoted():
    """`repr` and `ascii` pick single here; ruff wants double, which is why the
    quote cannot simply be left as Python's own choice."""
    from cliffracer.generate_client.emitter import _string_literal

    assert _string_literal("plain") == '"plain"'


# --- a nested model is written with a name the emitted import binds ---------
#
# `annotation_text` looked an alias up by the FULL qualname, while `_models` and
# the import loop only ever record the TOP-LEVEL name. For `Outer.Inner` the
# lookup therefore always missed, and the fallback invented
# `<Module><qualname>` -- `TestGenerateClientOuter.Inner` -- while the import
# line bound plain `Outer`. Every upstream gate passed: `type_ref` accepts a
# nested model and `unimportable_models` accepts it because every dotted part
# is an identifier, so the CLI exited 0 and wrote a file that raised NameError
# on import. That is the one thing the CLI promises never to do.
#
# The comment on `_models` has always said what should happen -- "a nested
# `X.Y` is imported as `X` and written `X.Y`" -- so this is the code being
# taught to match a stated intent, not a new feature.


class NestOuter(BaseModel):
    """Holder for a nested model. Its own field keeps it a real model."""

    name: str = "o"

    class Inner(BaseModel):
        sku: str = "s"


class NestedParam(CliffracerService):
    @rpc
    async def create(self, inner: NestOuter.Inner) -> None: ...


class NestedReturn(CliffracerService):
    @rpc
    async def fetch(self) -> NestOuter.Inner: ...


def test_a_nested_model_client_imports_and_keeps_the_nested_type(tmp_path):
    """The real chain, and the annotation read back from the imported module.

    Read off `inspect.signature` rather than the emitted text: the text is what
    looked plausible while the module would not load at all.
    """
    from cliffracer.introspect import describe

    src = emit(describe(NestedParam, service="nested", version="1"))
    mod, _ = _load(src, tmp_path, name="nested_param_client")

    annotation = inspect.signature(mod.NestedClient.create).parameters["inner"].annotation

    assert annotation is NestOuter.Inner, annotation


def test_a_nested_model_in_the_return_position_imports_too(tmp_path):
    """The return annotation goes through the same `annotation_text` call, but a
    separate one -- a fix applied to the parameter path only would leave this red."""
    from cliffracer.introspect import describe

    src = emit(describe(NestedReturn, service="nested", version="1"))
    mod, _ = _load(src, tmp_path, name="nested_return_client")

    assert inspect.signature(mod.NestedClient.fetch).return_annotation is NestOuter.Inner


def test_CONTROL_a_top_level_model_is_unchanged(tmp_path):
    """The nested fix must not rename the ordinary case, which is every other
    model in this suite: a name with no dot has no tail to re-attach."""
    from cliffracer.introspect import describe

    class TopLevelParam(CliffracerService):
        @rpc
        async def create(self, outer: NestOuter) -> None: ...

    src = emit(describe(TopLevelParam, service="top", version="1"))
    mod, _ = _load(src, tmp_path, name="top_level_client")

    assert inspect.signature(mod.TopClient.create).parameters["outer"].annotation is NestOuter


# --- a container default, and a service with nothing in it -------------------
#
# Two counterexamples to the module docstring's claim that the output is
# "formatted by construction to satisfy standard ruff formatting constraints".
# Both were invisible to the guards that assert it: `DESC` carried no container
# default, and every description those guards format has methods.
#
# The container default is also the one place the generated file cannot satisfy
# a rule this repository selects. `B006` fires on a mutable default, and the
# client's signature must mirror the service's -- so the default cannot be
# dropped, and the emitted file says the violation is deliberate.


class Containers(CliffracerService):
    @rpc
    async def search(
        self,
        tags: list[str] = ["a", "b"],  # noqa: B006 - the input under test
        opts: dict[str, str] = {"k": "v"},  # noqa: B006
        nested: list[list[str]] = [["x"]],  # noqa: B006
    ) -> str: ...


class OneContainer(CliffracerService):
    """One short parameter: the flat signature FITS inside the line limit.

    Every method on `Containers` is long enough to be exploded whatever the
    defaults are, so none of them reaches the clause that explodes a signature
    *because* it carries a suppression.
    """

    @rpc
    async def f(self, tags: list[str] = ["a"]) -> None: ...  # noqa: B006


EMPTY_DESC = {
    "service": "orders",
    "version": "1",
    "description_hash": "sha256:d",
    "methods": [],
}


def _lint(path: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(path)],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _format_check(path: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", str(path)],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


@pytest.fixture
def containers_client(tmp_path):
    from cliffracer.introspect import describe

    src = emit(describe(Containers, service="s", version="1"))
    mod, path = _load(src, tmp_path, name="containers_client")
    return mod, path, src


def test_a_container_default_reads_back_as_itself(containers_client):
    mod, _, _ = containers_client

    sig = inspect.signature(mod.SClient.search)

    assert sig.parameters["tags"].default == ["a", "b"]
    assert sig.parameters["opts"].default == {"k": "v"}
    assert sig.parameters["nested"].default == [["x"]]


def test_a_container_default_is_formatted_for_ruffs_defaults(containers_client, tmp_path):
    _, path, _ = containers_client

    assert _format_check(path, tmp_path).returncode == 0, _format_check(path, tmp_path).stdout


@pytest.mark.parametrize("where", ["repo", "isolated"])
def test_a_container_default_passes_lint_under_either_configuration(
    containers_client, tmp_path, where
):
    """`B006` is selected by this repository, so a generated file with a mutable
    default failed `ruff check` from here while passing it isolated -- and the
    guard above could not see it, because `DESC` had no container default."""
    _, path, _ = containers_client
    cwd = REPO if where == "repo" else tmp_path

    result = _lint(path, cwd)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_short_signature_with_a_container_default_is_still_exploded(tmp_path):
    """A suppression forces the exploded form even when the flat one would fit.

    The flat form cannot carry this: one trailing comment on it would cover
    every parameter, including any added later. Found by mutation -- dropping
    `not suppress` from the explode condition left all of the cases above green,
    because each of them has a signature too long to fit flat regardless, so
    none of them ever reached the clause.
    """
    from cliffracer.introspect import describe

    src = emit(describe(OneContainer, service="one", version="1"))
    _, path = _load(src, tmp_path, name="one_container_client")

    flat = '    async def f(self, tags: list[str] = ["a"]) -> None:'
    assert len(flat) <= 88, len(flat)
    assert flat not in src, src
    assert '        tags: list[str] = ["a"],  # noqa: B006' in src, src
    assert _lint(path, REPO).returncode == 0, _lint(path, REPO).stdout


def test_CONTROL_the_suppression_is_on_the_parameter_and_not_the_signature(containers_client):
    """One comment per offending parameter, after its comma.

    A single `# noqa: B006` on the flat signature line would cover every
    parameter including any added later, and a comment before the comma is a
    syntax error -- which is why a mutable default forces the exploded form.
    """
    _, _, src = containers_client

    assert 'tags: list[str] = ["a", "b"],  # noqa: B006' in src, src
    assert 'opts: dict[str, str] = {"k": "v"},  # noqa: B006' in src, src
    assert "async def search(\n" in src, "a mutable default must explode the signature"


def test_CONTROL_a_scalar_default_gets_no_suppression(tmp_path):
    """Otherwise the emitter could suppress B006 everywhere and pass the above."""
    desc = {
        "service": "s",
        "version": "1",
        "description_hash": "sha256:d",
        "methods": [
            {
                "name": "one",
                "doc": None,
                "signature_hash": "sha256:s",
                "params": [{"name": "n", "type": {"kind": "scalar", "name": "int"}, "default": 1}],
                "returns": {"kind": "scalar", "name": "str"},
            }
        ],
    }
    src = emit(Description.from_dict(desc))

    assert "noqa: B006" not in src, src


def test_a_service_with_no_methods_emits_an_empty_mapping(tmp_path):
    """`SIGNATURES = {` and `}` on two lines is what ruff collapses.

    With no entries there is no trailing comma to hold the exploded shape, so
    the formatter rewrites it -- the same reason `self._call` writes `{}` for a
    method with no parameters. `cliffracer-generate-client` refuses a
    description with no rpc methods before it reaches `emit`, so this pins the
    emitter's own output for a caller of `emit` that does not.
    """
    src = emit(Description.from_dict(EMPTY_DESC))

    assert "SIGNATURES: dict[str, str] = {}" in src, src
    mod, path = _load(src, tmp_path, name="empty_client")
    assert mod.SIGNATURES == {}
    assert _format_check(path, tmp_path).returncode == 0


@pytest.mark.parametrize("where", ["repo", "isolated"])
def test_a_service_with_no_methods_passes_lint(tmp_path, where):
    src = emit(Description.from_dict(EMPTY_DESC))
    _, path = _load(src, tmp_path, name="empty_lint_client")
    cwd = REPO if where == "repo" else tmp_path

    assert _lint(path, cwd).returncode == 0, _lint(path, cwd).stdout


def test_CONTROL_a_service_with_methods_still_explodes_its_mapping(tmp_path):
    """The empty case must not flatten the populated one: with entries the
    exploded form carries a trailing comma and is what ruff keeps."""
    src = emit(Description.from_dict(DESC))

    assert "SIGNATURES = {\n" in src, src
    assert "SIGNATURES = {}" not in src, src


# --- a parameter name cannot change what its own annotation means -----------
#
# The emitted body re-evaluated every annotation as an EXPRESSION inside the
# function, where the parameter names are in scope. `async def tag(self, list:
# list[str])` emitted `self._encode(list, list[str])`, which subscripts the
# caller's argument.
#
# Nothing upstream stops it: `list` is not in `dir(BaseModel)`, so the server
# accepts the handler, and `_refusals` screens a parameter name for
# identifier-ness, keywords, a leading underscore, `self` and `correlation_id`
# -- none of which a builtin's name trips. The file imports, lints clean and
# passes `verify()`.
#
# THE LOUD CASE IS THE LUCKY ONE. Only a parameter that shadows a SUBSCRIPTED
# annotation raises; shadowing a bare one silently substitutes the caller's
# argument for the type, and `_encode`/`_call` are handed a value where a type
# belongs. That is why these tests assert on what the client was PASSED rather
# than on the call not raising.
#
# No unit test here has ever called an emitted method -- they read the text,
# import it, or read `inspect.signature`. The end-to-end tier does call, but
# only with ordinary parameter names.


class _Recorder:
    """Stands in for the ServiceClient base, recording what the stub passes.

    It does no checking of its own: it records the annotation it is handed and
    returns the value unchanged, so an assertion reads the stub's behaviour and
    not this class's.
    """

    def __init__(self):
        self.encoded = []
        self.call = None

    def _encode(self, value, annotation):
        self.encoded.append((annotation, value))
        return value

    async def _call(self, method, params, return_type):
        self.call = (method, params, return_type)
        return "ok"


def _client_for(cls, tmp_path, name):
    from cliffracer.introspect import describe

    src = emit(describe(cls, service="shadow", version="1"))
    mod, _ = _load(src, tmp_path, name=name)
    client = mod.ShadowClient.__new__(mod.ShadowClient)
    rec = _Recorder()
    client._encode = rec._encode
    client._call = rec._call
    return client, rec


class ShadowsOwnAnnotation(CliffracerService):
    @rpc
    async def tag(self, list: list[str]) -> str: ...


class ShadowsTheReturnType(CliffracerService):
    @rpc
    async def tag(self, str: int) -> str: ...


class ShadowsAnotherParamsAnnotation(CliffracerService):
    @rpc
    async def tag(self, str: int, name: str) -> int: ...


def test_a_parameter_named_after_its_own_annotation_still_encodes_the_type(tmp_path):
    """`list: list[str]` subscripted the caller's argument and raised TypeError."""
    import asyncio

    client, rec = _client_for(ShadowsOwnAnnotation, tmp_path, "shadow_own_client")

    asyncio.run(client.tag(["a"]))

    assert rec.encoded == [(list[str], ["a"])], rec.encoded


def test_a_parameter_shadowing_the_return_type_leaves_the_return_type_alone(tmp_path):
    """The silent arm: this never raised, and `_call` got the ARGUMENT as its
    return type. A test asserting only that the call succeeds passes here with
    the defect present."""
    import asyncio

    client, rec = _client_for(ShadowsTheReturnType, tmp_path, "shadow_return_client")

    asyncio.run(client.tag(1))

    assert rec.call is not None
    assert rec.call[2] is str, f"return type was {rec.call[2]!r}"


def test_a_parameter_shadowing_another_parameters_annotation(tmp_path):
    """Also silent, and it is why a rule keyed on a parameter's OWN annotation
    is not enough: `str` here shadows the annotation of `name`, not its own."""
    import asyncio

    client, rec = _client_for(ShadowsAnotherParamsAnnotation, tmp_path, "shadow_other_client")

    asyncio.run(client.tag(1, "n"))

    assert rec.encoded == [(int, 1), (str, "n")], rec.encoded


class OrdinaryNames(CliffracerService):
    @rpc
    async def tag(self, items: list[str], count: int = 1) -> str: ...


def test_CONTROL_an_ordinary_signature_passes_the_same_types(tmp_path):
    """The fix must not change what a normal signature hands the base class."""
    import asyncio

    client, rec = _client_for(OrdinaryNames, tmp_path, "ordinary_client")

    asyncio.run(client.tag(["a"], 2))

    assert rec.encoded == [(list[str], ["a"]), (int, 2)], rec.encoded
    assert rec.call[2] is str


class ShadowsASubscriptedReturnType(CliffracerService):
    @rpc
    async def tag(self, list: int) -> list[str]: ...


def test_a_parameter_shadowing_a_subscripted_return_type_does_not_reach_the_cast(tmp_path):
    """The return alias is bound at module level for this. Written inline, the
    cast is `_typing.cast(list[str], _result)` inside the method, where `list`
    is the argument: `tag(1)` subscripts an int and raises TypeError after the
    reply has arrived. No other test here reaches the cast with a shadowed name."""
    import asyncio

    client, rec = _client_for(ShadowsASubscriptedReturnType, tmp_path, "shadow_cast_client")

    assert asyncio.run(client.tag(1)) == "ok"
    assert rec.call is not None and rec.call[2] == list[str], rec.call


class ShadowOrder(BaseModel):
    sku: str = "x"


class ShadowsAnImportedModelName(CliffracerService):
    @rpc
    async def tag(self, ShadowOrder: ShadowOrder) -> str: ...


class ShadowsTheLiteralImport(CliffracerService):
    @rpc
    async def tag(self, Literal: TLiteral["a", "b"]) -> str: ...


def test_a_parameter_named_after_an_imported_model_encodes_the_class(tmp_path):
    """Silent before: `_encode` was handed the model INSTANCE as the annotation,
    so the value was its own type and pydantic was asked to dump against an
    object. The generated file imported, linted and called without complaint."""
    import asyncio

    client, rec = _client_for(ShadowsAnImportedModelName, tmp_path, "shadow_model_client")
    order = ShadowOrder()

    asyncio.run(client.tag(order))

    assert rec.encoded == [(ShadowOrder, order)], rec.encoded


def test_a_parameter_named_literal_does_not_subscript_the_argument(tmp_path):
    """`Literal` is imported only when a literal annotation is used, so this is
    the same collision reached through the typing import rather than a model."""
    import asyncio

    client, rec = _client_for(ShadowsTheLiteralImport, tmp_path, "shadow_literal_client")

    asyncio.run(client.tag("a"))

    assert rec.encoded == [(TLiteral["a", "b"], "a")], rec.encoded


def test_a_service_with_no_methods_emits_collapsed_mappings(tmp_path):
    """An empty mapping written exploded is a diff on first contact with ruff.

    With no entries there is no trailing comma to hold the shape open, so the
    formatter collapses `_PARAM_TYPES = {\n}` to `_PARAM_TYPES = {}` and the
    generated file -- the one meant to be checked in and forgotten -- is
    reformatted by whoever next runs the formatter over their tree.

    The rule already existed for an empty `SIGNATURES` and for a method with no
    parameters. Adding two more mappings added two more places to reintroduce
    it, which is what happened.
    """

    class NoMethods(CliffracerService):
        pass

    from cliffracer.introspect import describe

    src = emit(describe(NoMethods, service="empty", version="1"))
    _, path = _load(src, tmp_path, name="empty_service_client")

    assert "_PARAM_TYPES: dict[str, dict[str, _typing.Any]] = {}" in src, src
    assert "_RETURN_TYPES: dict[str, _typing.Any] = {}" in src, src

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr


class LongParamName(CliffracerService):
    @rpc
    async def m(self, warehouse_identifier: str = "x") -> str: ...


def test_a_long_parameter_name_wraps_its_encode_call(tmp_path):
    """A table lookup is wider than the annotation it replaced.

    `_PARAM_TYPES["m"]["warehouse_identifier"]` stands where `str` used to, so a
    parameter name that fitted on one line before now crosses ruff's default
    limit and the formatter rewrites the file -- the generated client being a
    diff on first contact, which is the thing the emitter exists to avoid.

    Pinned here by name rather than left to a container-default shape that
    happens to have long parameters, because the reason those two coincide is
    not a reason either will stay that way.
    """
    from cliffracer.introspect import describe

    src = emit(describe(LongParamName, service="caps", version="1"))
    _, path = _load(src, tmp_path, name="long_param_client")

    assert '"warehouse_identifier": self._encode(\n' in src, src
    assert all(len(line) <= LINE_LENGTH for line in src.splitlines()), [
        line for line in src.splitlines() if len(line) > LINE_LENGTH
    ]

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- no model name can shadow what the generated file already binds ---------
#
# `emit` writes `from cliffracer.client import ServiceClient`, optionally
# `from typing import Literal`, then `SIGNATURES = {...}` and the client class
# -- and appended the model imports with no check that a model's name collides
# with any of them. `_refusals` guards the CLASS name against `ServiceClient`,
# so the collision class was known; the imported model names were never held to
# the same list.
#
# IMPORT FAILURE IS THE MILD END OF THIS. Only the two names bound BEFORE the
# models fail loudly. `SIGNATURES` and the client class are bound AFTER, so
# they quietly rebind the model's name, and the annotation that reaches
# `_encode` is a dict of signature hashes or the client class itself. The file
# imports, lints and calls without complaint.
#
# So the reserved set is derived from what `emit` actually writes, and
# `test_the_reserved_set_is_every_name_the_file_binds` reads the emitted output
# back by AST to prove the two have not drifted. A new module-level name added
# without adding it to the set reopens this hole in silence, which is how it
# got here.


class ServiceClient(BaseModel):
    """A user model whose name is the transport base class."""

    sku: str = "x"


class Literal(BaseModel):  # noqa: F811 - deliberately shadows the typing import
    """A user model whose name is the typing import."""

    sku: str = "x"


class SIGNATURES(BaseModel):
    """A user model whose name is the module-level signature table."""

    sku: str = "x"


class CollideClient(BaseModel):
    """A user model whose name is the class `emit` generates for service `collide`."""

    sku: str = "x"


class Innocent(BaseModel):
    sku: str = "x"


def _collide_service(model, with_literal=False):
    if with_literal:

        class Collide(CliffracerService):
            @rpc
            async def a(self, m: model, k: TLiteral["x", "y"] = "x") -> str: ...
    else:

        class Collide(CliffracerService):
            @rpc
            async def a(self, m: model) -> str: ...

    return Collide


def _annotation_the_client_passes(model, tmp_path, name, with_literal=False):
    """Emit, import, CALL, and report the annotation handed to `_encode`.

    Importing is not the test: two of these collisions import cleanly and pass
    the wrong type at call time.
    """
    import asyncio

    from cliffracer.introspect import describe

    src = emit(describe(_collide_service(model, with_literal), service="collide", version="1"))
    mod, _ = _load(src, tmp_path, name=name)
    client = mod.CollideClient.__new__(mod.CollideClient)
    rec = _Recorder()
    client._encode = rec._encode
    client._call = rec._call
    args = (model(), "x") if with_literal else (model(),)
    asyncio.run(mod.CollideClient.a(client, *args))
    return mod, rec.encoded[0][0]


@pytest.mark.parametrize(
    ("model", "name", "with_literal"),
    [
        (ServiceClient, "collide_base", False),
        (Literal, "collide_literal", True),
        (SIGNATURES, "collide_signatures", False),
        (CollideClient, "collide_classname", False),
        (Innocent, "collide_none", False),
    ],
    ids=["ServiceClient", "Literal", "SIGNATURES", "the client class", "CONTROL innocent"],
)
def test_a_model_named_after_an_emitted_name_still_reaches_encode(
    model, name, with_literal, tmp_path
):
    """Whatever the model is called, `_encode` gets the model class."""
    _, annotation = _annotation_the_client_passes(model, tmp_path, name, with_literal)

    assert annotation is model, annotation


def test_the_generated_class_still_subclasses_the_transport_base(tmp_path):
    """The loud half: a model named `ServiceClient` made the client subclass the
    user's pydantic model, and the file did not import at all."""
    from cliffracer.client import ServiceClient as RealServiceClient

    mod, _ = _annotation_the_client_passes(ServiceClient, tmp_path, "collide_base_mro")

    assert issubclass(mod.CollideClient, RealServiceClient)


def _module_level_bindings(src: str, *, skip_modules: set[str] | None = None) -> list[str]:
    """Every name the generated source binds at module level, in order, WITH
    repeats.

    A list rather than a set, because a name can be bound twice and a set
    cannot see it. Every statement form that binds a name is walked: an
    unformatted insertion reds the formatting tests instead, which reads like
    the fence working when it is not, so a form left out of this walk is
    invisible exactly when the addition is well-formed.
    """
    skip = skip_modules or set()
    out: list[str] = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.ImportFrom):
            if node.module in skip:
                continue
            out += [alias.asname or alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            out += [alias.asname or alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.Assign):
            out += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                out.append(node.target.id)
        elif isinstance(node, ast.TypeAlias):
            out.append(node.name.id)
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            out.append(node.name)
    return out


def test_the_reserved_set_is_every_name_the_file_binds(tmp_path):
    """The fence, and the reason this is not a hard-coded list.

    Reads the emitted source back by AST and compares the names it binds --
    other than the model imports -- against the set the emitter reserves. A new
    module-level name added to `emit` without adding it to the reserved set
    reopens the hole silently; this reds instead.
    """
    from cliffracer.generate_client.emitter import _reserved_module_names
    from cliffracer.introspect import describe

    desc = describe(_collide_service(Innocent, with_literal=True), service="collide", version="1")
    src = emit(desc)

    bound = _module_level_bindings(src, skip_modules={"tests.unit.test_generate_client"})

    assert set(bound) == _reserved_module_names(desc, uses_literal=True), (
        f"emitted {sorted(set(bound))}, "
        f"reserved {sorted(_reserved_module_names(desc, uses_literal=True))}"
    )


def test_no_name_is_bound_twice_in_a_generated_file(tmp_path):
    """Set equality cannot see a duplicate, and a duplicate is reachable.

    Two modules exporting one model name are both aliased, and a prefixed alias
    can land on a name the file already binds: modules `m` and `n` exporting
    `SClient`, for service `m_s` whose client class is `MSClient`, aliased
    `m.SClient` to exactly `MSClient`. The file imported and called correctly --
    the annotation tables are bound above the class statement -- so the damage
    was to an outside importer asking for `MSClient` and getting the client.
    """
    from cliffracer.generate_client.emitter import _class_name, _module_prefix
    from cliffracer.introspect import describe
    from tests.fixtures.dupnames import modm, modn

    class MS(CliffracerService):
        @rpc
        async def a(self, x: modm.SClient, y: modn.SClient) -> str: ...

    # The service name is DERIVED so the collision actually happens: the alias
    # for `modm` is `_module_prefix(module) + "SClient"`, and this service's
    # class name is exactly that. Chosen by hand it does not collide, and the
    # first version of this test asserted no duplicates in a case that never
    # had one -- green for the same reason the defect is invisible.
    service = "tests_fixtures_dupnames_modm_s"
    assert _class_name(service) == _module_prefix(modm.__name__) + "SClient", (
        "the collision this test exists for is not being constructed"
    )

    src = emit(describe(MS, service=service, version="1"))
    bound = _module_level_bindings(src, skip_modules=set())

    duplicates = sorted({n for n in bound if bound.count(n) > 1})
    assert not duplicates, f"bound twice: {duplicates} in {bound}"


# --- a docstring the emitter wraps like everything else ----------------------
#
# `ruff format` does not reflow the contents of a string, so an over-long
# docstring passes `ruff format --check` and only `E501` sees it. That is why
# the formatting guard structurally could not catch this, and why the
# line-length guard above needed a long doc rather than a new assertion.

DOC_SHAPES = [
    ("one_paragraph", LONG_DOC),
    ("two_paragraphs", LONG_DOC + "\n\nThe identifier is stable across retries of one batch."),
    ("ends_with_a_quote", 'The flag is spelled "'),
    # The short case above takes the single-line path and never reaches the
    # multi-line assembly, so it cannot observe where the closing quotes go.
    # Found by mutation: moving them onto the last content line reded nothing.
    (
        "long_and_ends_with_a_quote",
        "Reconcile the ledger against the manifest and return the run identifier, "
        'which the operator reads as "',
    ),
    (
        "holds_triple_quotes",
        'A doc with """ inside it, long enough to need wrapping onto a second line.',
    ),
    (
        "holds_a_backslash",
        "A path like C:\\temp is spelled with a backslash, and this one wraps.",
    ),
]


def _client_for_doc(doc: str, tmp_path: Path, name: str):
    desc = Description.from_dict(
        {**LONG_DOC_DESC, "methods": [{**LONG_DOC_DESC["methods"][0], "doc": doc}]}
    )
    src = emit(desc)
    mod, path = _load(src, tmp_path, name=name)
    return mod, path, src


@pytest.mark.parametrize(("name", "doc"), DOC_SHAPES, ids=[d[0] for d in DOC_SHAPES])
def test_a_docstring_is_wrapped_to_the_target(name, doc, tmp_path):
    _, _, src = _client_for_doc(doc, tmp_path, f"doc_{name}_client")

    too_long = [
        (number, len(line))
        for number, line in enumerate(src.splitlines(), 1)
        if len(line) > LINE_LENGTH
    ]

    assert not too_long, (too_long, src)


@pytest.mark.parametrize(("name", "doc"), DOC_SHAPES, ids=[d[0] for d in DOC_SHAPES])
def test_a_wrapped_docstring_keeps_every_word(name, doc, tmp_path):
    """Read off `__doc__` after importing, not off the emitted text.

    Wrapping is allowed to move the line breaks and nothing else, so the
    comparison is on whitespace-normalised words. Reading the source instead
    would pass for a doc that was escaped into something that no longer means
    the same thing -- the backslash and triple-quote shapes are exactly that
    risk, and they round-trip through `_escape_docstring_text`.
    """
    mod, _, _ = _client_for_doc(doc, tmp_path, f"words_{name}_client")

    emitted = mod.LedgerClient.reconcile.__doc__

    assert " ".join((emitted or "").split()) == " ".join(doc.split()), emitted


@pytest.mark.parametrize(("name", "doc"), DOC_SHAPES, ids=[d[0] for d in DOC_SHAPES])
def test_a_wrapped_docstring_needs_no_reformatting(name, doc, tmp_path):
    """`ruff format` rewrites a single-content-line docstring that ends in a
    quote, which is why the closing quotes get a line of their own."""
    _, path, _ = _client_for_doc(doc, tmp_path, f"fmt_{name}_client")

    assert _format_check(path, tmp_path).returncode == 0, _format_check(path, tmp_path).stdout


@pytest.mark.parametrize(("name", "doc"), DOC_SHAPES, ids=[d[0] for d in DOC_SHAPES])
def test_a_wrapped_docstring_passes_the_line_length_rule(name, doc, tmp_path):
    """The rule that actually saw this defect, at the width the emitter targets."""
    _, path, _ = _client_for_doc(doc, tmp_path, f"e501_{name}_client")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--select",
            "E501",
            "--line-length",
            "88",
            str(path),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_CONTROL_a_short_docstring_still_emits_on_one_line(tmp_path):
    """The common case must not grow three lines because of this change.

    Every doc in the committed fixture is short, so a change that wrapped
    unconditionally would rewrite output nobody asked to change and would not
    be caught by any assertion about length.
    """
    _, _, src = _client_for_doc("Create an order.", tmp_path, "short_doc_client")

    assert '        """Create an order."""' in src, src


def test_an_unbreakable_token_is_left_whole(tmp_path):
    """A single token longer than the width is NOT cut in half.

    Measured, and this is why: `E501` does not flag a line whose overlong part
    carries no whitespace, while it does flag a 159-character line of ordinary
    words. So leaving the token whole keeps the file clean under the rule the
    generated file is measured by, and breaking it would trade a line the
    linter accepts for a URL that no longer resolves.
    """
    url = "https://example.invalid/" + "x" * 90
    mod, path, src = _client_for_doc(
        f"See {url} for the full description.", tmp_path, "url_doc_client"
    )

    assert url in src, "the token was broken across lines"
    assert any(len(line) > LINE_LENGTH for line in src.splitlines()), (
        "this test is only meaningful while the token does exceed the target"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--select",
            "E501",
            "--line-length",
            "88",
            str(path),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert url in (mod.LedgerClient.reconcile.__doc__ or "")


# --- the emitted import block is sorted ---------------------------------------
#
# `ruff format` does not sort imports, so `ruff format --check` returns 0 on an
# unsorted block and only `ruff check` (I001, which this repository selects)
# ever sees it. Every module in `DESC` and in the committed fixture is under
# `tests.`, which sorts after `cliffracer`, so the guards that lint the
# generated file had no description whose imports could be out of order.
#
# The lint runs with `cwd` set to the generated file's OWN directory, not the
# repository root. From inside this repo ruff resolves `cliffracer` and `tests`
# as first-party and wants a section split; in a consumer tree neither is
# first-party and the whole block is one section. The consumer tree is the case
# the CLI promises, so that is the cwd these checks use.

IMPORT_SHAPES = [
    ("sorts_before_the_transport", ["appmodels.m"]),
    ("sorts_before_with_a_dot", ["acme.schemas"]),
    ("sorts_after_the_transport", ["zmodels.m"]),
    # Upper case, sorting AFTER the transport case-insensitively but BEFORE it
    # in ASCII. This is the shape a plain `sorted()` gets wrong -- `Z` < `c` --
    # and the first version of this fix emitted it first and reported I001.
    ("upper_case_sorting_after", ["Zmodels.m"]),
    ("upper_case_shouting", ["DMODELS.m"]),
    ("both_sides_of_the_transport", ["appmodels.m", "zmodels.m"]),
    ("case_differing_only", ["Bmodels.m", "bmodels2.m"]),
    ("the_committed_fixture_module", ["tests.fixtures.typed_client.models"]),
]


def _desc_with_modules(modules, *, literal=False):
    params = [
        {
            "name": f"x{index}",
            "type": {"kind": "model", "module": module, "qualname": "Order"},
            "default": None,
            "required": True,
        }
        for index, module in enumerate(modules)
    ]
    if literal:
        params.append(
            {
                "name": "k",
                "type": {"kind": "literal", "values": ["a", "b"]},
                "default": None,
                "required": True,
            }
        )
    return Description.from_dict(
        {
            "service": "orders",
            "version": "1",
            "description_hash": "sha256:d",
            "methods": [
                {
                    "name": "go",
                    "signature_hash": "sha256:s",
                    "doc": None,
                    "params": params,
                    "returns": {"kind": "scalar", "name": "str"},
                }
            ],
        }
    )


def _import_sort_check(path: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--isolated", "--select", "I001", str(path)],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _write_client(src: str, tmp_path: Path, name: str) -> Path:
    """Write the generated source WITHOUT importing it.

    These shapes name modules that do not exist -- `appmodels.m`, `Zmodels.m`
    -- because the question is how the import block is ordered, which is a
    property of the text. `_load` execs the module and would raise
    `ModuleNotFoundError` before any check ran. The import path itself is
    covered by the tests that use the committed fixture's real modules.
    """
    path = tmp_path / f"{name}.py"
    path.write_text(src)
    return path


def _emitted_module_order(src: str) -> list[str]:
    return [
        line.split()[1]
        for line in src.splitlines()
        if line.startswith("from ") and " import " in line
    ]


@pytest.mark.parametrize(("name", "modules"), IMPORT_SHAPES, ids=[s[0] for s in IMPORT_SHAPES])
def test_the_emitted_import_block_passes_ruffs_import_sort(name, modules, tmp_path):
    src = emit(_desc_with_modules(modules))
    path = _write_client(src, tmp_path, f"imports_{name}_client")

    result = _import_sort_check(path, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(("name", "modules"), IMPORT_SHAPES, ids=[s[0] for s in IMPORT_SHAPES])
def test_the_emitted_import_block_is_sorted_case_insensitively(name, modules, tmp_path):
    """The decision, read off the emitted text rather than off ruff's verdict.

    Ruff's isort is case-insensitive by default and Python's `sorted` is not,
    so the two disagree exactly where a module name's case differs from the
    transport's. Asserting the property directly means a future change of sort
    key reds here by name rather than as an I001 code from a subprocess.
    """
    src = emit(_desc_with_modules(modules))

    order = _emitted_module_order(src)

    assert "cliffracer.client" in order, order
    assert order == sorted(order, key=lambda module: (module.lower(), module)), order


def test_a_stdlib_import_stays_in_its_own_section(tmp_path):
    """`from typing import Literal` is a different isort section, so it stays
    first with a blank line after it however the rest of the block sorts."""
    src = emit(_desc_with_modules(["appmodels.m"], literal=True))
    path = _write_client(src, tmp_path, "imports_literal_client")

    lines = src.splitlines()
    typing_at = lines.index("from typing import Literal")

    assert lines[typing_at + 1] == "", lines[typing_at : typing_at + 3]
    assert _import_sort_check(path, tmp_path).returncode == 0


def test_CONTROL_the_formatter_cannot_see_an_unsorted_import_block(tmp_path):
    """Why no formatting guard could have caught this, stated executably.

    The unsorted block is written by hand, because the emitter no longer
    produces one. `ruff format --check` accepts it and I001 refuses it, which
    is exactly how the defect survived a guard asserting the generated file is
    already formatted.
    """
    path = tmp_path / "unsorted.py"
    path.write_text(
        '"""Hand-written, deliberately unsorted."""\n'
        "\n"
        "from cliffracer.client import ServiceClient\n"
        "from appmodels.m import Order\n"
        "\n"
        "USED = (ServiceClient, Order)\n"
    )

    formatted = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "--isolated", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert formatted.returncode == 0, formatted.stdout + formatted.stderr
    assert _import_sort_check(path, tmp_path).returncode == 1, "I001 no longer refuses this"


def test_CONTROL_the_import_block_is_byte_identical_across_two_emits():
    """Sorting must not have introduced an order that varies between runs."""
    desc = _desc_with_modules(["Bmodels.m", "bmodels2.m", "appmodels.m"])

    first, second = emit(desc), emit(desc)

    assert first == second
    assert _emitted_module_order(first) == _emitted_module_order(second)

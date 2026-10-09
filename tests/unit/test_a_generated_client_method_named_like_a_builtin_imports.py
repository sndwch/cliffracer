"""A generated client for a service with an RPC method named `list`, `dict` or `str` imports.

A `def` evaluates its annotations in the class body, where the methods already defined are names. The
emitter bound annotations at module level so a parameter could not shadow them, but a sibling method
could: after `async def list(...)`, the next signature's `list[str]` is the method, and the import
raised `TypeError: 'function' object is not subscriptable`. A name that is both bound in the class
and read by one of its annotations is now read through a module-level alias bound before the class.
"""

import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from cliffracer.generate_client.emitter import CannotEmit, emit
from cliffracer.introspect import Description, describe
from tests.fixtures.shadowing_catalog import NAMESPACE, VERSION, Catalog, OnlyInsideOptional, Scoped

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def _source() -> str:
    return emit(describe(Catalog, service="catalog", version="1"))


def _load(source: str, tmp_path: Path, name: str = "catalog_client"):
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, path


def test_the_client_imports(tmp_path):
    module, _ = _load(_source(), tmp_path)

    assert module.CatalogClient.__name__ == "CatalogClient"


def test_every_method_is_there_and_the_class_attributes_are_what_they_were(tmp_path):
    module, _ = _load(_source(), tmp_path)
    client = module.CatalogClient

    for name in ("list", "dict", "str", "search", "lookup"):
        assert inspect.iscoroutinefunction(getattr(client, name)), name
    assert client.SERVICE == "catalog" and client.VERSION == "1"


def test_each_annotation_is_the_class_it_names_and_not_a_sibling_method(tmp_path):
    module, _ = _load(_source(), tmp_path)
    client = module.CatalogClient

    assert inspect.signature(client.search).parameters["tags"].annotation == list[str]
    assert inspect.signature(client.search).parameters["limit"].annotation is int
    assert inspect.signature(client.dict).parameters["filters"].annotation == dict[str, int]
    assert inspect.signature(client.dict).return_annotation == dict[str, int]
    assert inspect.signature(client.str).parameters["text"].annotation is str
    assert inspect.signature(client.list).return_annotation == list[str]
    assert inspect.signature(client.lookup).parameters["key"].annotation is VERSION
    assert inspect.signature(client.lookup).return_annotation == list[VERSION]


def test_only_the_names_that_are_shadowed_are_aliased(tmp_path):
    source = _source()

    assert "_Unshadowed_list = list" in source
    assert "_Unshadowed_dict = dict" in source
    assert "_Unshadowed_str = str" in source
    assert "_Unshadowed_VERSION = VERSION" in source
    assert "_Unshadowed_int" not in source and "_Unshadowed_search" not in source


def test_the_generated_file_is_clean_for_ruff_and_strict_mypy(tmp_path, mypy_cache):
    _, path = _load(_source(), tmp_path)

    for command in (
        [sys.executable, "-m", "ruff", "check", str(path)],
        [sys.executable, "-m", "ruff", "format", "--check", str(path)],
    ):
        result = subprocess.run(command, capture_output=True, text=True, cwd=tmp_path)
        assert result.returncode == 0, (command, result.stdout + result.stderr)
    config = tmp_path / "mypy.ini"
    config.write_text("[mypy]\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(config),
            "--strict",
            "--cache-dir",
            str(mypy_cache),
            "--follow-imports=silent",
            str(path),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_model_named_like_a_class_attribute_that_no_signature_reads_is_not_aliased(tmp_path):
    # CONTROL: a client whose methods share no name with an annotation is written as it always was.
    description = describe(Catalog, service="catalog", version="1").to_dict()
    description["methods"] = [m for m in description["methods"] if m["name"] in {"search"}]
    source = emit(Description.from_dict(description))

    assert "_Unshadowed_" not in source
    assert "async def search(self, tags: list[str], limit: int = 5) -> int:" in source
    path = tmp_path / "catalog_client.py"
    path.write_text(source)
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--isolated", "--check", str(path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_model_named_namespace_is_read_as_itself_when_the_client_has_no_namespace():
    """With no namespace the class binds no `NAMESPACE`, so nothing shadows the model."""
    source = emit(describe(Scoped, service="scoped", version="1"))

    assert "_Unshadowed_" not in source
    assert "    async def get(self, key: NAMESPACE) -> NAMESPACE:\n" in source


def test_a_method_named_literal_beside_a_literal_annotation_is_refused_by_name():
    description = describe(Catalog, service="catalog", version="1").to_dict()
    template = next(m for m in description["methods"] if m["name"] == "str")
    literal = {"kind": "literal", "values": ["a", "b"]}
    extra = {
        **template,
        "name": "Literal",
        "params": [{**template["params"][0], "name": "mode", "type": literal}],
    }
    description["methods"] = [*description["methods"], extra]

    with pytest.raises(CannotEmit, match="Literal"):
        emit(Description.from_dict(description))


def test_a_model_named_namespace_is_read_through_an_alias_when_the_client_has_a_namespace(
    tmp_path,
):
    """`NAMESPACE = "acme"` is bound in the class, so a bare `NAMESPACE` annotation reads the string."""
    source = emit(describe(Scoped, service="scoped", version="1"), namespace="acme")

    module, _ = _load(source, tmp_path, "scoped_client")

    client = module.ScopedClient
    assert client.NAMESPACE == "acme"
    assert "_Unshadowed_NAMESPACE = NAMESPACE" in source
    assert inspect.signature(client.get).parameters["key"].annotation is NAMESPACE
    assert inspect.signature(client.get).return_annotation is NAMESPACE


def test_a_name_read_only_inside_an_optional_is_still_read_through_an_alias(tmp_path):
    """`list` is a method of the class and `tags: list[str] | None` reads it inside the `| None`."""
    source = emit(describe(OnlyInsideOptional, service="only_optional", version="1"))

    module, _ = _load(source, tmp_path, "only_optional_client")

    client = module.OnlyOptionalClient
    assert "_Unshadowed_list = list" in source
    assert inspect.signature(client.search).parameters["tags"].annotation == (list[str] | None)


def _builtin_named_methods_and(*methods: dict) -> Description:
    """`list` and `str` as methods of the client, beside `methods`, which read them."""
    description = describe(Catalog, service="catalog", version="1").to_dict()
    template = next(m for m in description["methods"] if m["name"] == "str")
    integer = {"kind": "scalar", "name": "int"}
    description["methods"] = [
        {**template, "name": "list", "params": [], "returns": integer},
        {**template, "name": "str", "params": [], "returns": integer},
        *({**template, **method} for method in methods),
    ]
    return Description.from_dict(description)


LIST_OF_TEXT = {"kind": "list", "item": {"kind": "scalar", "name": "str"}}


def test_a_name_read_only_by_a_return_annotation_is_aliased(tmp_path):
    """No parameter reads `list` or `str` here: only `names`' return annotation does."""
    description = _builtin_named_methods_and(
        {"name": "names", "params": [], "returns": LIST_OF_TEXT}
    )

    module, _ = _load(emit(description), tmp_path, "returns_client")

    assert inspect.signature(module.CatalogClient.names).return_annotation == list[str]


def test_a_name_read_only_inside_a_subscript_is_aliased(tmp_path):
    """`str` is read only as the argument of `list[...]` in a parameter's annotation."""
    description = _builtin_named_methods_and(
        {
            "name": "tags",
            "params": [{"name": "names", "type": LIST_OF_TEXT}],
            "returns": {"kind": "scalar", "name": "int"},
        }
    )

    module, _ = _load(emit(description), tmp_path, "subscript_client")

    assert inspect.signature(module.CatalogClient.tags).parameters["names"].annotation == list[str]


def test_a_name_read_only_by_a_later_argument_of_a_subscript_is_aliased(tmp_path):
    """`list` is read only as the second argument of `dict[str, list[int]]`: every argument of a
    subscript is read, not only its first."""
    integer = {"kind": "scalar", "name": "int"}
    description = _builtin_named_methods_and(
        {
            "name": "counts",
            "params": [
                {
                    "name": "by_name",
                    "type": {
                        "kind": "dict",
                        "value": {"kind": "list", "item": integer},
                    },
                }
            ],
            "returns": integer,
        }
    )

    module, _ = _load(emit(description), tmp_path, "later_argument_client")

    annotation = inspect.signature(module.CatalogClient.counts).parameters["by_name"].annotation
    assert annotation == dict[str, list[int]]

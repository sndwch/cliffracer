"""A parameter typed as a model, with a default the service can rebuild, has a model for its default.

The description carries a model's default as the dict it dumps to, and the client used to write that
dict as the default: `item: Item = {"name": "x", ...}`. It ran, because `_encode` accepts the dict, and
it was a type error for the consumer (`mypy --strict`: `Incompatible default for argument "item"`),
and `inspect.signature` showed a dict where it promised an `Item`.

When the service has said the default is `rebuildable` (see the tests of that), the default is
`Item.model_validate({...}, strict=False)`, and a list or dict of models has each member built. The
instance is made once, when the client module is imported, and every call that leaves the argument
out passes that one object, which `_encode` only reads. Each expression ruff reports as B008 carries
`# noqa: B008` on the line that opens it, and a list or dict default carries `# noqa: B006` on its
opening line: a comment after a closing bracket is not honoured. This file is about the layout and
the generated code; the decision is the service's.
"""

import asyncio
import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from cliffracer.generate_client.cli import main
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import Description
from tests.fixtures.model_defaults import (
    AVeryLongNamedModelThatTheCatalogueUsesForItsEntriesInTheStore,
    Bounded,
    Box,
    Item,
    Stock,
    Strictness,
    Unioned,
)

pytestmark = pytest.mark.unit

CLASS = f"tests.fixtures.model_defaults:{Stock.__name__}"
MODULE = "tests.fixtures.model_defaults"
STR = {"kind": "scalar", "name": "str"}


def _ref(model: type[BaseModel]) -> dict:
    """A model's TypeRef as `describe` writes it."""
    return {"kind": "model", "module": MODULE, "qualname": model.__qualname__}


ITEM = _ref(Item)
BOX = _ref(Box)
LONG_MODEL = AVeryLongNamedModelThatTheCatalogueUsesForItsEntriesInTheStore
LONG = LONG_MODEL.__name__


def _generate(tmp_path: Path) -> str:
    path = tmp_path / "stock_client.py"
    assert main(["--class", CLASS, "--service", "stock", "--version", "1", "--out", str(path)]) == 0
    return path.read_text()


def _desc(params: list[dict]) -> Description:
    return Description.from_dict(
        {
            "service": "stock",
            "version": "1",
            "description_hash": "sha256:d",
            "methods": [
                {
                    "name": "m",
                    "doc": None,
                    "signature_hash": "sha256:m",
                    "params": params,
                    "returns": STR,
                }
            ],
        }
    )


def _param(name: str, type_: dict, default, rebuildable: bool | None = True) -> dict:
    """A parameter with a default; `rebuildable` is what the service says of it, None for no word."""
    param = {"name": name, "type": type_, "default": default}
    if rebuildable is not None:
        param["rebuildable"] = rebuildable
    return param


def _ruff(source: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    path = tmp_path / "client.py"
    path.write_text(source)
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args, str(path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )


def _clean(source: str, tmp_path: Path) -> None:
    """Lint with B006 and B008 selected, formatted, and within the width.

    The width is read off every line but the hashes of the signature table, which a formatter cannot
    split and which are not what is under test.
    """
    lint = _ruff(
        source, tmp_path, "check", "--isolated", "--select", "E,F,I,B,UP,RUF100", "--ignore", "E501"
    )
    fmt = _ruff(source, tmp_path, "format", "--isolated", "--check")
    assert lint.returncode == 0, lint.stdout + lint.stderr
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr
    for line in source.splitlines():
        code = line.split("  # noqa")[0]
        assert "sha256:" in code or len(code) <= 88, line


def _clean_but_for_the_width(source: str, tmp_path: Path) -> None:
    """A line that cannot be shorter is left long by ruff; the rest is as `_clean`."""
    lint = _ruff(
        source, tmp_path, "check", "--isolated", "--select", "E,F,I,B,UP,RUF100", "--ignore", "E501"
    )
    fmt = _ruff(source, tmp_path, "format", "--isolated", "--check")
    assert lint.returncode == 0, lint.stdout + lint.stderr
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr


def _import(source: str, tmp_path: Path):
    path = tmp_path / "stock_client_under_test.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("stock_client_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the client for a service whose handlers have these defaults ---------------------------------


def test_a_model_default_is_built_with_model_validate_and_suppressed_where_ruff_reports_it(
    tmp_path,
):
    source = _generate(tmp_path)

    assert (
        "        item: Item = Item.model_validate(  # noqa: B008\n"
        '            {"color": "red", "name": "x", "tags": ["a"]}, strict=False\n'
        "        ),\n"
    ) in source
    assert 'item: Item = {"name"' not in source
    _clean(source, tmp_path)


def test_the_signature_shows_a_model_where_it_promised_one(tmp_path):
    module = _import(_generate(tmp_path), tmp_path)
    parameters = inspect.signature(module.StockClient.one).parameters
    boxed = inspect.signature(module.StockClient.boxed).parameters
    maybe = inspect.signature(module.StockClient.maybe).parameters

    assert parameters["item"].default == Item(name="x", tags=["a"])
    assert type(parameters["item"].default) is Item
    assert boxed["box"].default == Box(label="l", item=Item(name="inner"))
    assert maybe["item"].default is None
    assert type(maybe["other"].default) is Item


def test_a_list_and_a_dict_of_models_have_each_member_built(tmp_path):
    module = _import(_generate(tmp_path), tmp_path)

    many = inspect.signature(module.StockClient.many).parameters["items"].default
    by_name = inspect.signature(module.StockClient.by_name).parameters["items"].default

    assert [type(m) for m in many] == [Item, Item]
    assert [m.name for m in many] == ["a", "b"]
    assert {k: type(v) for k, v in by_name.items()} == {"first": Item}


@pytest.mark.parametrize(
    ("ref", "default", "check"),
    [
        pytest.param(
            {"kind": "list", "item": {"kind": "optional", "inner": ITEM}},
            [None, {"name": "a", "color": "red", "tags": []}],
            lambda value: value[0] is None and type(value[1]) is Item,
            id="list-with-a-none",
        ),
        pytest.param(
            {"kind": "dict", "value": {"kind": "optional", "inner": ITEM}},
            {"a": None, "b": {"name": "b", "color": "red", "tags": []}},
            lambda value: value["a"] is None and type(value["b"]) is Item,
            id="dict-with-a-none",
        ),
    ],
)
def test_a_container_mixing_models_and_literals_has_each_model_built(ref, default, check, tmp_path):
    """A member that builds no model is written as its literal beside the members that do."""
    source = emit(_desc([_param("xs", ref, default)]))

    _clean(source, tmp_path)
    assert source.count("Item.model_validate(") == 1
    built = inspect.signature(_import(source, tmp_path).StockClient.m).parameters["xs"].default
    assert check(built), built


def test_a_default_is_one_object_shared_by_every_call_that_leaves_the_argument_out(tmp_path):
    module = _import(_generate(tmp_path), tmp_path)

    first = inspect.signature(module.StockClient.one).parameters["item"].default
    second = inspect.signature(module.StockClient.one).parameters["item"].default

    assert first is second


def test_the_default_is_encoded_as_the_dict_it_was_described_as(tmp_path):
    module = _import(_generate(tmp_path), tmp_path)
    client = module.StockClient(nats_url="nats://broker.invalid:6999", verify=False)
    default = inspect.signature(module.StockClient.one).parameters["item"].default

    encoded = client._encode(default, module._PARAM_TYPES["one"]["item"])

    assert encoded == {"name": "x", "color": "red", "tags": ["a"]}


def test_CONTROL_a_parameter_with_no_model_in_it_is_written_as_before(tmp_path):
    source = emit(_desc([_param("tags", {"kind": "list", "item": STR}, ["a", "b"])]))

    assert 'tags: list[str] = ["a", "b"],  # noqa: B006' in source
    assert "model_validate" not in source


def test_CONTROL_a_none_default_for_an_optional_model_stays_none(tmp_path):
    source = emit(_desc([_param("item", {"kind": "optional", "inner": ITEM}, None)]))

    assert "item: Item | None = None" in source
    assert "model_validate" not in source


def test_a_client_written_with_the_dict_default_is_reported_stale_by_check(tmp_path):
    path = tmp_path / "stock_client.py"
    path.write_text(_generate(tmp_path))
    old = path.read_text().replace(
        "        item: Item = Item.model_validate(  # noqa: B008\n"
        '            {"color": "red", "name": "x", "tags": ["a"]}, strict=False\n'
        "        ),\n",
        '        item: Item = {"color": "red", "name": "x", "tags": ["a"]},  # noqa: B006\n',
    )
    assert old != path.read_text()
    path.write_text(old)

    code = main(
        ["--class", CLASS, "--service", "stock", "--version", "1", "--out", str(path), "--check"]
    )

    assert code == 8


def test_a_default_the_importing_projects_model_does_not_accept_fails_the_import(tmp_path):
    """The keys fit the schema, and the value does not satisfy the model's own constraint."""
    source = emit(_desc([_param("bounded", _ref(Bounded), {"count": 0})]))

    with pytest.raises(ValidationError, match="count"):
        _import(source, tmp_path)


# --- every length, in every shape ----------------------------------------------------------------


@pytest.mark.parametrize("length", [1, 8, 20, 29, 30, 31, 35, 45, 60])
def test_a_model_default_of_any_length_is_formatted_suppressed_and_within_the_width(
    length, tmp_path
):
    default = {"name": "n" * length, "color": "red", "tags": []}

    source = emit(_desc([_param("item", ITEM, default)]))

    _clean(source, tmp_path)
    assert "item: Item = Item.model_validate(" in source


@pytest.mark.parametrize("fields", [1, 2, 4, 8])
@pytest.mark.parametrize("width", [10, 40])
def test_a_model_with_many_fields_explodes_into_one_entry_per_line(fields, width, tmp_path):
    default = {f"field_{i}": "v" * width for i in range(fields)}
    default["name"] = "n"

    source = emit(_desc([_param("item", ITEM, default)]))

    _clean(source, tmp_path)


@pytest.mark.parametrize("count", [1, 2, 3, 6])
@pytest.mark.parametrize("name_length", [1, 12, 40])
def test_a_list_of_models_of_any_length_is_formatted_and_each_call_is_suppressed(
    count, name_length, tmp_path
):
    items = [{"name": f"{i}" * name_length, "color": "red", "tags": []} for i in range(count)]

    source = emit(_desc([_param("items", {"kind": "list", "item": ITEM}, items)]))

    _clean(source, tmp_path)
    assert source.count("noqa: B008") >= count - (1 if count == 1 else 0)


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("key_length", [3, 30])
def test_a_dict_of_models_of_any_length_is_formatted_and_each_call_is_suppressed(
    count, key_length, tmp_path
):
    entries = {
        f"{i}" * key_length: {"name": "n" * 20, "color": "red", "tags": ["t"] * 3}
        for i in range(count)
    }

    source = emit(_desc([_param("items", {"kind": "dict", "value": ITEM}, entries)]))

    _clean(source, tmp_path)


@pytest.mark.parametrize("count", [3, 9, 20])
def test_a_list_inside_a_model_default_is_split_as_ruff_splits_it(count, tmp_path):
    default = {"name": "n", "color": "red", "tags": [f"tag-number-{i}" for i in range(count)]}

    source = emit(_desc([_param("item", ITEM, default)]))

    _clean(source, tmp_path)


@pytest.mark.parametrize("length", range(24, 44))
def test_a_default_whose_line_is_at_the_width_is_left_on_one_line(length, tmp_path):
    """Somewhere in this range the one-line form is exactly 88 columns, and ruff keeps it joined."""
    default = {"name": "n" * length, "color": "red", "tags": []}

    source = emit(_desc([_param("item", ITEM, default)]))

    _clean(source, tmp_path)


@pytest.mark.parametrize("name_length", range(40, 54))
def test_a_signature_of_plain_parameters_is_one_line_up_to_the_width(name_length, tmp_path):
    """The flat form of `async def m(self, x: int) -> str:` is one line up to 88 columns, which a
    52-character name reaches, and one parameter to a line past it; either form is clean."""
    name = "p" * name_length

    source = emit(_desc([{"name": name, "type": {"kind": "scalar", "name": "int"}}]))

    if name_length <= 52:
        assert f"    async def m(self, {name}: int) -> str:\n" in source
    else:
        assert f"    async def m(\n        self,\n        {name}: int,\n    ) -> str:\n" in source
    _clean(source, tmp_path)


def test_a_list_of_models_inside_a_dict_default_asks_for_no_suppression_it_does_not_need(tmp_path):
    default = {"first": [{"name": "a", "color": "red", "tags": []}]}

    source = emit(
        _desc([_param("items", {"kind": "dict", "value": {"kind": "list", "item": ITEM}}, default)])
    )

    _clean(source, tmp_path)
    assert source.count("# noqa: B006") == 1


def test_a_list_default_on_one_line_carries_b006_beside_the_b008_of_its_members(tmp_path):
    source = emit(_desc([_param("items", {"kind": "list", "item": ITEM}, [{"name": "n"}])]))

    assert (
        '        items: list[Item] = [Item.model_validate({"name": "n"}, strict=False)],'
        "  # noqa: B006, B008\n"
    ) in source
    _clean(source, tmp_path)


def test_a_list_inside_a_model_default_asks_for_no_b006_it_does_not_need(tmp_path):
    """Only the parameter's own list or dict is a mutable default; one inside a model is not."""
    default = {"label": "l" * 50, "rows": [[1, "a"]], "by_key": {}}

    source = emit(_desc([_param("u", _ref(Unioned), default)]))

    _clean(source, tmp_path)
    assert source.count("# noqa: B006") == 0


def test_the_arguments_are_on_one_line_at_exactly_88_columns_and_one_to_a_line_past_it(tmp_path):
    """The comment is not measured. A trailing comma would keep a split form, so the text is read."""
    joined = emit(_desc([_param("item", ITEM, {"name": "n" * 22, "color": "red", "tags": []})]))
    apart = emit(_desc([_param("item", ITEM, {"name": "n" * 23, "color": "red", "tags": []})]))

    line = '            {"color": "red", "name": "' + "n" * 22 + '", "tags": []}, strict=False'
    assert len(line) == 88
    assert line + "\n" in joined
    assert "        item: Item = Item.model_validate(  # noqa: B008\n" in apart
    assert '            {"color": "red", "name": "' + "n" * 23 + '", "tags": []},\n' in apart
    assert "            strict=False,\n        ),\n" in apart
    _clean(joined, tmp_path)
    _clean(apart, tmp_path)


def test_a_model_default_argument_is_one_line_at_exactly_88_columns_and_split_past_it(tmp_path):
    """The dict a split call holds on a line of its own is laid out by the same width rule."""
    joined = emit(_desc([_param("item", ITEM, {"name": "n" * 35, "color": "red", "tags": []})]))
    apart = emit(_desc([_param("item", ITEM, {"name": "n" * 36, "color": "red", "tags": []})]))

    line = '            {"color": "red", "name": "' + "n" * 35 + '", "tags": []},'
    assert len(line) == 88
    assert line + "\n" in joined
    assert "            {\n" in apart and '                "name": "' + "n" * 36 + '",\n' in apart
    _clean(joined, tmp_path)
    _clean(apart, tmp_path)


def test_a_signature_of_plain_parameters_is_one_line_at_88_columns_and_split_past_it():
    flat = emit(_desc([{"name": "p" * 52, "type": {"kind": "scalar", "name": "int"}}]))
    split = emit(_desc([{"name": "p" * 53, "type": {"kind": "scalar", "name": "int"}}]))

    line = "    async def m(self, " + "p" * 52 + ": int) -> str:"
    assert len(line) == 88
    assert line + "\n" in flat
    assert "    async def m(\n" in split


def test_a_dict_of_lists_of_models_inside_a_list_asks_for_no_suppression_it_does_not_need(
    tmp_path,
):
    inner = {"name": "a", "color": "red", "tags": []}
    ref = {"kind": "list", "item": {"kind": "dict", "value": {"kind": "list", "item": ITEM}}}

    source = emit(_desc([_param("items", ref, [{"k": [inner]}])]))

    _clean(source, tmp_path)
    assert source.count("# noqa: B006") == 1


@pytest.mark.parametrize("depth", range(1, 12))
def test_a_default_under_an_annotation_of_any_depth_is_formatted_and_suppressed(depth, tmp_path):
    """From the depth where the annotation alone fills the line, ruff splits the annotation first
    and the default stands whole on the line that closes it."""
    ref, value = ITEM, {"name": "a", "color": "red", "tags": []}
    for _ in range(depth):
        ref, value = {"kind": "list", "item": ref}, [value]

    source = emit(_desc([_param("items", ref, value)]))

    _clean_but_for_the_width(source, tmp_path)


@pytest.mark.parametrize("name_length", range(1, 13))
def test_a_default_is_formatted_whatever_the_width_left_for_its_opening(name_length, tmp_path):
    """Over these lengths the annotation and ` = [` are exactly 88 columns for one, and 89 for the next."""
    ref, value = ITEM, {"name": "a", "color": "red", "tags": []}
    for _ in range(10):
        ref, value = {"kind": "list", "item": ref}, [value]

    source = emit(_desc([_param("p" * name_length, ref, value)]))

    _clean_but_for_the_width(source, tmp_path)


@pytest.mark.parametrize("fields", [{"name": "a"}, {"name": "a" * 40}])
def test_a_model_with_a_very_long_name_is_built_through_it(fields, tmp_path):
    long_model = _ref(LONG_MODEL)

    source = emit(_desc([_param("entry", long_model, fields)]))

    _clean_but_for_the_width(source, tmp_path)
    assert f"entry: {LONG} = {LONG}.model_validate(  # noqa: B008\n" in source


def test_an_optional_model_with_a_very_long_name_and_a_default_is_formatted_and_suppressed(
    tmp_path,
):
    ref = {"kind": "optional", "inner": _ref(LONG_MODEL)}

    source = emit(_desc([_param("entry", ref, {"name": "a"})]))

    _clean_but_for_the_width(source, tmp_path)


def test_a_model_holding_a_model_is_built_from_the_nested_dict(tmp_path):
    default = {"label": "l" * 30, "item": {"name": "n" * 30, "color": "red", "tags": ["a", "b"]}}

    source = emit(_desc([_param("box", BOX, default)]))

    _clean(source, tmp_path)
    assert source.count("model_validate") == 1


def test_several_defaults_in_one_signature_are_each_laid_out(tmp_path):
    source = emit(
        _desc(
            [
                _param("a", ITEM, {"name": "a", "color": "red", "tags": []}),
                _param("b", ITEM, {"name": "b" * 50, "color": "red", "tags": []}),
                _param(
                    "c", {"kind": "list", "item": ITEM}, [{"name": "c", "color": "r", "tags": []}]
                ),
                _param("d", {"kind": "list", "item": STR}, ["x"]),
            ]
        )
    )

    _clean(source, tmp_path)
    assert source.count("B008") == 3
    assert source.count("# noqa: B006") == 2


def test_a_default_after_one_without_a_default_keeps_its_keyword_only_marker(tmp_path):
    source = emit(
        _desc(
            [
                _param("a", ITEM, {"name": "a", "color": "red", "tags": []}),
                {"name": "b", "type": STR},
            ]
        )
    )

    _clean(source, tmp_path)
    assert "        *,\n        b: str,\n" in source


def test_the_keyword_only_marker_is_written_once_and_only_after_a_default(tmp_path):
    item = {"name": "a", "color": "red", "tags": []}
    source = emit(
        _desc(
            [
                _param("a", ITEM, item),
                {"name": "b", "type": STR},
                {"name": "c", "type": STR},
            ]
        )
    )

    _clean(source, tmp_path)
    assert source.count("        *,\n") == 1
    assert "        *,\n        b: str,\n        c: str,\n" in source

    no_default_first = emit(_desc([{"name": "b", "type": STR}, _param("a", ITEM, item)]))
    assert "*," not in no_default_first

    only_defaults = emit(_desc([_param("a", ITEM, item), _param("b", ITEM, item)]))
    _clean(only_defaults, tmp_path)
    assert "*," not in only_defaults, "a marker before a parameter that has a default"


def test_a_built_default_whose_leaf_is_too_long_for_the_line_keeps_the_leaf_whole(tmp_path):
    source = emit(_desc([_param("item", ITEM, {"name": "n" * 80, "color": "red", "tags": []})]))

    _clean_but_for_the_width(source, tmp_path)
    assert "item: Item = Item.model_validate(" in source


# --- names ----------------------------------------------------------------------------------------


def test_a_model_whose_name_is_a_method_of_the_client_is_built_through_its_alias(tmp_path):
    desc = Description.from_dict(
        {
            "service": "stock",
            "version": "1",
            "description_hash": "sha256:d",
            "methods": [
                {
                    "name": "Item",
                    "doc": None,
                    "signature_hash": "sha256:i",
                    "params": [],
                    "returns": STR,
                },
                {
                    "name": "m",
                    "doc": None,
                    "signature_hash": "sha256:m",
                    "params": [_param("item", ITEM, {"name": "x", "color": "red", "tags": []})],
                    "returns": STR,
                },
            ],
        }
    )

    source = emit(desc)
    module = _import(source, tmp_path)

    assert "_Unshadowed_Item.model_validate(" in source
    assert type(inspect.signature(module.StockClient.m).parameters["item"].default) is Item
    _clean(source, tmp_path)


# --- a consumer that checks types ------------------------------------------------------------------


CONSUMER = """from stock_client import StockClient
from tests.fixtures.model_defaults import Item


async def run(client: StockClient) -> None:
    a: str = await client.one()
    b: str = await client.one(Item(name="y"))
    c: int = await client.many()
    d: int = await client.by_name()
    e: str = await client.boxed()
    print(a, b, c, d, e)
"""


def test_the_client_and_a_consumer_pass_strict_mypy(tmp_path, mypy_cache):
    workspace = tmp_path / "check"
    workspace.mkdir()
    (workspace / "stock_client.py").write_text(_generate(tmp_path))
    (workspace / "consumer.py").write_text(CONSUMER)
    (workspace / "mypy.ini").write_text("[mypy]\n")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(workspace / "mypy.ini"),
            "--strict",
            "--cache-dir",
            str(mypy_cache),
            "--follow-imports=silent",
            str(workspace / "stock_client.py"),
            str(workspace / "consumer.py"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# --- the service decides ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rebuildable", "built"),
    [(True, True), (False, False), (None, False)],
    ids=["the service says it can be", "the service says it cannot", "an older description"],
)
def test_a_default_is_built_only_when_the_description_says_it_can_be_rebuilt(
    rebuildable, built, tmp_path
):
    default = {"name": "x", "color": "red", "tags": []}

    source = emit(_desc([_param("item", ITEM, default, rebuildable)]))

    assert ("Item.model_validate(" in source) is built
    if not built:
        assert 'item: Item = {"color": "red", "name": "x", "tags": []},  # noqa: B006' in source
    _import(source, tmp_path)


def test_a_list_of_models_is_built_member_by_member_on_the_one_flag(tmp_path):
    members = [{"name": "a", "color": "red", "tags": []}, {"name": "b", "color": "red", "tags": []}]
    ref = {"kind": "list", "item": ITEM}

    built = emit(_desc([_param("items", ref, members, True)]))
    kept = emit(_desc([_param("items", ref, members, False)]))

    assert built.count("Item.model_validate(") == 2
    assert "model_validate" not in kept


# --- a model declared strict ----------------------------------------------------------------------

STRICTNESS = f"tests.fixtures.model_defaults:{Strictness.__name__}"


def _generate_strictness(tmp_path: Path) -> str:
    path = tmp_path / "strictness_client.py"
    args = ["--class", STRICTNESS, "--service", "strictness", "--version", "1", "--out", str(path)]
    assert main(args) == 0
    return path.read_text()


def test_a_client_for_strict_models_imports_and_builds_each_default(tmp_path):
    """The default is the service's JSON-mode dump: a datetime is a string, a set a list.

    `model_validate` of that, strict, refuses it ("Input should be a valid datetime"). Built at
    import that failed the whole client module, where the dict only failed its own call.
    """
    source = _generate_strictness(tmp_path)

    module = _import(source, tmp_path)

    assert source.count("model_validate(") == 6
    assert source.count("strict=False") == 6
    _clean(source, tmp_path)
    built = inspect.signature(module.StrictnessClient.model_level).parameters["value"].default
    assert type(built).__name__ == "StrictModel"
    assert built.tags == {"t"} and built.pair == (3, 4) and built.raw == b"x"


def test_the_payload_of_a_strict_default_is_the_dump_the_service_described(tmp_path):
    module = _import(_generate_strictness(tmp_path), tmp_path)
    client = module.StrictnessClient(nats_url="nats://broker.invalid:6999", verify=False)
    sent = {}

    async def record(method, params, return_type):
        sent[method] = params
        return 1

    client._call = record

    for name in ("model_level", "field_level", "annotated", "many", "by_name", "money"):
        asyncio.run(getattr(client, name)())

    when = "2026-01-02T03:04:05"
    assert sent["model_level"] == {
        "value": {
            "when": when,
            "ident": "00000000-0000-0000-0000-000000000001",
            "shade": "blue",
            "tags": ["t"],
            "raw": "x",
            "pair": [3, 4],
        }
    }
    assert sent["field_level"] == {"value": {"when": when}}
    assert sent["annotated"] == {"value": {"when": when}}
    assert sent["many"] == {"values": [{"when": when}]}
    assert sent["by_name"] == {"values": {"k": {"when": when}}}
    assert sent["money"] == {"value": {"amount": "1.5"}}


def test_CONTROL_a_lax_model_is_built_the_same_with_the_argument(tmp_path):
    default = {"name": "x", "color": "red", "tags": []}
    module = _import(emit(_desc([_param("item", ITEM, default)])), tmp_path)

    built = inspect.signature(module.StockClient.m).parameters["item"].default

    assert built == Item(name="x")

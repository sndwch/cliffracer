"""The generated client is lint-clean and formatted by construction in the shapes that were not.

`emit` promises output that needs no formatter and trips no linter, and held it for the fixture the
tests used. Four shapes did not hold, and a fifth was a hole in what `emit` refused:

1. A mutable default split over several lines carried `# noqa: B006` after its closing bracket, and
   ruff reports B006 on the line that opens the default, so the suppression was not honoured.
2. Two models of one name from different modules gave `from shop.models import Item as ShopModelsItem,
   Order`, which ruff's isort wants as two statements: I001.
3. A docstring of 75 to 77 characters was written with its closing quotes alone on a line, which
   `ruff format` rejoins, and one-line it is over the width, which E501 reports.
5. Duplicate parameter names, names that are one name once Python normalises them, duplicate method
   names and a NUL in a version, a hash or a doc made a file that does not compile, or one that
   silently shadows a method.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from cliffracer.generate_client.emitter import CannotEmit, emit
from cliffracer.introspect import Description

pytestmark = pytest.mark.unit

STR = {"kind": "scalar", "name": "str"}


def _desc(methods: list[dict], **extra) -> Description:
    return Description.from_dict(
        {
            "service": "lint",
            "version": "1",
            "description_hash": "sha256:d",
            "methods": methods,
            **extra,
        }
    )


def _method(name="m", params=(), returns=None, doc=None) -> dict:
    return {
        "name": name,
        "doc": doc,
        "signature_hash": "sha256:" + name,
        "params": list(params),
        "returns": returns or STR,
    }


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
    lint = _ruff(
        source, tmp_path, "check", "--isolated", "--select", "E,F,I,B,UP", "--ignore", "E501,B008"
    )
    fmt = _ruff(source, tmp_path, "format", "--isolated", "--check")
    long = _ruff(source, tmp_path, "check", "--isolated", "--select", "E501")
    assert lint.returncode == 0, lint.stdout + lint.stderr
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr
    assert long.returncode == 0, long.stdout + long.stderr


# --- 1. the suppression on a split default ---------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 4, 8, 12])
def test_a_list_default_of_any_length_is_suppressed_where_ruff_reports_it(count, tmp_path):
    tags = [f"tag-number-{i}" for i in range(count)]
    param = {"name": "tags", "type": {"kind": "list", "item": STR}, "default": tags}
    source = emit(_desc([_method(params=[param])]))

    _clean(source, tmp_path)
    assert "noqa: B006" in source


def test_a_dict_default_that_splits_is_suppressed_where_ruff_reports_it(tmp_path):
    default = {f"key-number-{i}": f"value-number-{i}" for i in range(6)}
    param = {
        "name": "mapping",
        "type": {"kind": "dict", "value": STR},
        "default": default,
    }

    _clean(emit(_desc([_method(params=[param])])), tmp_path)


def test_CONTROL_a_short_default_stays_on_one_line_with_its_suppression(tmp_path):
    param = {"name": "tags", "type": {"kind": "list", "item": STR}, "default": ["a", "b"]}
    source = emit(_desc([_method(params=[param])]))

    _clean(source, tmp_path)
    assert 'tags: list[str] = ["a", "b"],  # noqa: B006' in source


# --- 2. imports from one module, plain and aliased --------------------------------------------------


def _models(modules: dict[str, list[str]]) -> Description:
    params = []
    for module, names in modules.items():
        for name in names:
            params.append(
                {
                    "name": f"{name.lower()}_{module.replace('.', '_')}",
                    "type": {"kind": "model", "module": module, "qualname": name},
                }
            )
    return _desc([_method(params=params)])


def test_a_plain_name_that_sorts_first_makes_the_plain_statement_first(tmp_path):
    source = emit(_models({"shop.models": ["Alpha", "Item"], "depot.models": ["Item"]}))

    lint = _ruff(source, tmp_path, "check", "--isolated", "--select", "I001")

    assert lint.returncode == 0, lint.stdout + lint.stderr
    assert source.index("from shop.models import Alpha\n") < source.index(
        "from shop.models import Item as ShopModelsItem\n"
    )


def test_a_module_with_a_plain_and_an_aliased_name_is_two_statements(tmp_path):
    # `Item` is in two modules, so it is aliased; `Order` is not.
    source = emit(_models({"shop.models": ["Item", "Order"], "depot.models": ["Item"]}))
    path = tmp_path / "client.py"
    path.write_text(source)

    lint = _ruff(source, tmp_path, "check", "--isolated", "--select", "I001")

    assert lint.returncode == 0, lint.stdout + lint.stderr
    assert "from shop.models import Order\n" in source
    assert "from shop.models import Item as ShopModelsItem\n" in source
    assert "import Item as ShopModelsItem, Order" not in source


def test_several_aliased_names_from_one_module_are_a_statement_each(tmp_path):
    source = emit(
        _models(
            {
                "shop.models": ["Item", "Line", "Order"],
                "depot.models": ["Item", "Line"],
            }
        )
    )

    lint = _ruff(source, tmp_path, "check", "--isolated", "--select", "I001")

    assert lint.returncode == 0, lint.stdout + lint.stderr


def test_CONTROL_a_module_with_only_plain_names_is_still_one_statement(tmp_path):
    source = emit(_models({"shop.models": ["Item", "Order"]}))

    lint = _ruff(source, tmp_path, "check", "--isolated", "--select", "I001")

    assert lint.returncode == 0, lint.stdout + lint.stderr
    assert "from shop.models import Item, Order\n" in source


# --- 3. a docstring of 75 to 77 characters ------------------------------------------------------------


@pytest.mark.parametrize("length", list(range(60, 100)))
def test_a_docstring_of_any_length_is_formatted_and_within_the_width(length, tmp_path):
    words = ("alpha beta gamma delta epsilon zeta eta theta iota kappa " * 4)[:length].rstrip()
    doc = words + "x" * (length - len(words))
    assert len(doc) == length

    _clean(emit(_desc([_method(doc=doc)])), tmp_path)


def test_a_docstring_with_no_space_in_it_is_left_whole(tmp_path):
    doc = "x" * 76

    source = emit(_desc([_method(doc=doc)]))

    assert f'"""{doc}"""' in source
    assert _ruff(source, tmp_path, "format", "--isolated", "--check").returncode == 0


def test_CONTROL_a_short_docstring_is_one_line_and_a_long_one_wraps(tmp_path):
    short = emit(_desc([_method(doc="Create an order.")]))
    long = emit(_desc([_method(doc="word " * 60)]))

    assert '        """Create an order."""\n' in short
    _clean(long, tmp_path)


# --- 5. what emit refuses ------------------------------------------------------------------------------


def test_duplicate_parameter_names_are_refused_by_name():
    params = [{"name": "a", "type": STR}, {"name": "a", "type": STR}]

    with pytest.raises(CannotEmit, match=r"m\.a"):
        emit(_desc([_method(params=params)]))


def test_parameter_names_that_are_one_name_once_python_reads_them_are_refused():
    # U+FB01 is the "fi" ligature, which NFKC reads as "fi".
    params = [{"name": "file", "type": STR}, {"name": "ﬁle", "type": STR}]

    with pytest.raises(CannotEmit, match="m"):
        emit(_desc([_method(params=params)]))


def test_duplicate_method_names_are_refused_by_name():
    with pytest.raises(CannotEmit, match="dup"):
        emit(_desc([_method("dup"), _method("dup")]))


def test_method_names_that_are_one_name_once_python_reads_them_are_refused():
    with pytest.raises(CannotEmit):
        emit(_desc([_method("file"), _method("ﬁle")]))


@pytest.mark.parametrize(
    ("field", "value"),
    [("version", "1\x00"), ("description_hash", "sha256:\x00")],
)
def test_a_nul_in_the_version_or_the_hash_is_refused(field, value):
    base = {"service": "lint", "version": "1", "description_hash": "sha256:d", "methods": []}
    base[field] = value

    with pytest.raises(CannotEmit, match="NUL"):
        emit(Description.from_dict(base))


def test_a_nul_in_a_doc_is_refused_by_the_method():
    with pytest.raises(CannotEmit, match="NUL"):
        emit(_desc([_method("m", doc="before\x00after")]))


def test_CONTROL_distinct_names_and_clean_text_still_emit(tmp_path):
    params = [{"name": "a", "type": STR}, {"name": "b", "type": STR}]

    source = emit(_desc([_method("one", params=params), _method("two")]))

    compile(source, "client.py", "exec")
    _clean(source, tmp_path)

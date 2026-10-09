"""A describe reply is text from whoever answered, and none of it becomes code in the generated file.

`{service}.describe` is a plain subject with no owner, so the reply is not trusted. The generator
writes a model's module into an `import` line, and a module that held a newline put statements into
the file, which ran when the file was imported. A module and a qualname are now refused unless they
are dotted paths of plain identifiers, by the one rule the command's exit 4 and `emit` share, and a
refusal escapes the text it names instead of printing it.
"""

import json
import subprocess
import sys

import pytest

from cliffracer.core.typed_rpc import unimportable_models
from cliffracer.generate_client import cli
from cliffracer.generate_client.emitter import CannotEmit, emit
from cliffracer.introspect import Description

pytestmark = pytest.mark.unit

MARKER = "INJECTED"


def _reply(module, qualname="Path", *, service="svc"):
    return {
        "service": service,
        "version": "1",
        "methods": [
            {
                "name": "m",
                "doc": None,
                "params": [
                    {
                        "name": "x",
                        "type": {"kind": "model", "module": module, "qualname": qualname},
                    }
                ],
                "returns": {"kind": "scalar", "name": "int"},
                "signature_hash": "sha256:h",
            }
        ],
    }


def _injection(marker_path):
    # The shape that worked: a newline ends the import, and a statement follows it.
    return f"os import getcwd\nopen({str(marker_path)!r}, 'w').write('x')\nfrom os"


HOSTILE_MODULES = [
    pytest.param("os import getcwd\nprint(1)\nfrom os", id="a-newline-and-a-statement"),
    pytest.param("os; import sys", id="a-semicolon"),
    pytest.param("os import x #", id="a-trailing-comment"),
    pytest.param("shop.models import Item as Order, Foo", id="an-import-clause"),
    pytest.param("a b", id="a-space"),
    pytest.param("class", id="a-keyword"),
    pytest.param("shop.import.models", id="a-keyword-part"),
    pytest.param("a..b", id="an-empty-part"),
    pytest.param(".relative", id="a-leading-dot"),
    pytest.param("1abc", id="a-leading-digit"),
    pytest.param("", id="empty"),
    pytest.param("a.b\x00", id="a-nul"),
    pytest.param("a. b", id="a-unicode-line-separator"),
    pytest.param("a.b\x1b[2K", id="an-escape-sequence"),
    pytest.param(123, id="not-a-string"),
    pytest.param(None, id="none"),
]


@pytest.mark.parametrize("module", HOSTILE_MODULES)
def test_a_module_that_is_not_a_dotted_identifier_path_is_refused_by_emit(module):
    description = Description.from_dict(_reply(module))

    with pytest.raises(CannotEmit):
        emit(description)


@pytest.mark.parametrize("module", HOSTILE_MODULES)
def test_the_commands_own_check_refuses_it_before_emit_is_reached(module):
    assert cli._unimportable(Description.from_dict(_reply(module)))


@pytest.mark.parametrize(
    "qualname",
    ["Path\nimport os", "a b", "class", "A..B", "", "A.B;C", "A. B"],
)
def test_a_qualname_that_is_not_a_dotted_identifier_path_is_refused(qualname):
    assert unimportable_models({"kind": "model", "module": "shop.models", "qualname": qualname})


@pytest.mark.parametrize(
    ("module", "qualname"),
    [
        ("shop.models", "Order"),
        ("shop.models", "Outer.Inner"),
        ("a", "A"),
        ("acme.v2.models", "Order"),
        ("ünïcode.models", "Order"),
    ],
)
def test_CONTROL_a_dotted_path_of_identifiers_is_still_importable(module, qualname):
    assert unimportable_models({"kind": "model", "module": module, "qualname": qualname}) == []
    emit(Description.from_dict(_reply(module, qualname)))


@pytest.mark.parametrize(
    "module", ["_private.models", "shop._models", "__main__", "shop.models.__init__"]
)
def test_CONTROL_a_private_module_is_still_refused(module):
    assert unimportable_models({"kind": "model", "module": module, "qualname": "Order"})


def test_a_refusal_names_the_text_without_printing_a_control_character():
    refused = unimportable_models({"kind": "model", "module": "a\nb\x1b[2K", "qualname": "C\rD"})

    (line,) = refused
    assert line.isprintable() and "\n" not in line and "\x1b" not in line
    assert "a\\nb" in line


def test_the_emit_refusal_is_one_printable_line_even_for_a_hostile_method_name():
    reply = _reply("shop.models")
    reply["methods"][0]["name"] = "m\nimport os"

    with pytest.raises(CannotEmit) as refused:
        emit(Description.from_dict(reply))

    assert str(refused.value).isprintable()


def test_the_command_refuses_a_hostile_reply_and_nothing_it_wrote_runs(
    tmp_path, monkeypatch, capsys
):
    marker = tmp_path / MARKER
    out = tmp_path / "svc_client.py"

    async def fake_fetch(*args, **kwargs):
        return json.dumps(_reply(_injection(marker))).encode()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fake_fetch)

    code = cli.main(["--service", "svc", "--out", str(out)])

    err = capsys.readouterr().err
    # If a file was written at all, importing it is what would run the reply's statements.
    if out.exists():
        subprocess.run(
            [sys.executable, "-c", "import svc_client"], cwd=tmp_path, capture_output=True
        )
    assert not marker.exists(), "the generated file ran the describe reply's statements"
    assert code == 4 and not out.exists()
    assert len(err.strip().splitlines()) == 1, err
    assert "open(" in err and "\\n" in err  # named, with the newline escaped


def test_the_commands_own_check_refuses_a_hostile_return_model_before_emit_is_reached(
    tmp_path, monkeypatch, capsys
):
    reply = _reply("shop.models")
    reply["methods"][0]["params"] = []
    reply["methods"][0]["returns"] = {"kind": "model", "module": "os; import sys", "qualname": "X"}
    reached: list[bool] = []

    async def fake_fetch(*args, **kwargs):
        return json.dumps(reply).encode()

    def emit_must_not_run(*args, **kwargs):
        reached.append(True)
        raise AssertionError("emit was reached")

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fake_fetch)
    monkeypatch.setattr("cliffracer.generate_client.cli.emit", emit_must_not_run)

    code = cli.main(["--service", "svc", "--out", str(tmp_path / "svc_client.py")])

    err = capsys.readouterr().err
    assert code == 4 and reached == []
    assert "Move these models into an importable package" in err


def test_the_command_writes_a_client_that_imports_for_an_honest_reply(
    tmp_path, monkeypatch, capsys
):
    out = tmp_path / "honest_client.py"
    models = tmp_path / "honest_models.py"
    models.write_text("from pydantic import BaseModel\n\nclass Item(BaseModel):\n    n: int = 0\n")

    async def fake_fetch(*args, **kwargs):
        return json.dumps(_reply("honest_models", "Item")).encode()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fake_fetch)

    code = cli.main(["--service", "svc", "--out", str(out)])

    assert code == 0
    run = subprocess.run(
        [sys.executable, "-c", "import honest_client; print(honest_client.SvcClient.__name__)"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "SvcClient"


def test_CONTROL_the_probe_sees_code_that_runs_at_import(tmp_path):
    """The marker check can fail: a file that writes the marker when imported is seen to."""
    marker = tmp_path / MARKER
    (tmp_path / "bad_client.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")

    subprocess.run([sys.executable, "-c", "import bad_client"], cwd=tmp_path, check=True)

    assert marker.exists()

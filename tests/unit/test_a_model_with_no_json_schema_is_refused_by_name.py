"""A handler that takes or returns a model with no JSON Schema is refused by name.

The contract is published as JSON Schema, and pydantic cannot write one for a model with a field
it cannot describe, such as a callable. That error used to escape `build_handler_spec`, and so
`describe` and the generator, as pydantic's own `PydanticInvalidForJsonSchema`, naming neither the
handler nor the parameter, where every other refusal in the module names `Owner.handler` and the
parameter or the return. The service refuses to start with the named error, `describe` raises it,
and `cliffracer-generate-client` reports it as exit 4, "the service cannot be described".
"""

import sys

import pytest

from cliffracer.core.typed_rpc import UntypedHandler
from cliffracer.generate_client.cli import main
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit

MODULE = """
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from cliffracer import CliffracerService, ServiceConfig, listener, rpc


class Hook(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    callback: Callable[[int], int] | None = None


class Plain(BaseModel):
    n: int = 0


class TakesIt(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="takes_it", subject_prefix=None, health_port=0))

    @rpc
    async def process(self, hook: Hook) -> int:
        return 1


class ReturnsIt(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="returns_it", subject_prefix=None, health_port=0))

    @rpc
    async def make(self) -> Hook:
        return Hook()


class HearsIt(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="hears_it", subject_prefix=None, health_port=0))

    @listener("things.created")
    async def on_created(self, subject: str, hook: Hook) -> None:
        return None


class Fine(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="fine", subject_prefix=None, health_port=0))

    @rpc
    async def process(self, plain: Plain) -> Plain:
        return plain
"""


@pytest.fixture
def shapes(tmp_path, monkeypatch):
    (tmp_path / "no_schema_shapes.py").write_text(MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "no_schema_shapes", raising=False)
    import no_schema_shapes

    return no_schema_shapes


@pytest.mark.parametrize(
    ("cls", "names"),
    [
        ("TakesIt", ["TakesIt.process", "parameter 'hook'", "Hook has no JSON Schema"]),
        ("ReturnsIt", ["ReturnsIt.make", "return", "Hook has no JSON Schema"]),
        ("HearsIt", ["HearsIt.on_created", "parameter 'hook'", "Hook has no JSON Schema"]),
    ],
)
def test_describe_refuses_the_handler_by_name(shapes, cls, names):
    with pytest.raises(UntypedHandler) as caught:
        describe(getattr(shapes, cls))

    for text in names:
        assert text in str(caught.value), str(caught.value)


def test_the_message_does_not_carry_pydantics_documentation_url(shapes):
    with pytest.raises(UntypedHandler) as caught:
        describe(shapes.TakesIt)

    assert "errors.pydantic.dev" not in str(caught.value)


@pytest.mark.parametrize("cls", ["TakesIt", "ReturnsIt", "HearsIt"])
def test_the_generator_reports_it_as_exit_4(shapes, capsys, tmp_path, cls):
    out = tmp_path / "client.py"

    rc = main(["--class", f"no_schema_shapes:{cls}", "--service", "x", "--out", str(out)])

    captured = capsys.readouterr()
    assert rc == 4, captured.err
    assert "has no JSON Schema" in captured.err
    assert "Traceback" not in captured.err
    assert not out.exists()


def test_CONTROL_a_service_whose_models_have_schemas_still_describes(shapes):
    description = describe(shapes.Fine)

    assert [m.name for m in description.methods] == ["process"]

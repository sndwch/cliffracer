"""Every example in a decorator's docstring declares handlers a service accepts.

These docstrings are what an editor shows on hover, so they are the most-read
examples of the decorators. Each `Example:` block is declared on a real service
class and put through handler discovery, which is where an untyped event
handler, an undeclared listener or a durable without JetStream is refused.

Bodies are replaced with `...`: an example may call anything, and what this
pins is the declaration. A name an example uses as an annotation but does not
define -- `OrderCreated` -- is supplied as an empty pydantic model, because the
reader's own model is what the example stands for.
"""

import ast
import builtins
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

import cliffracer
from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.repo

DECORATORS = Path(cliffracer.__file__).parent / "core" / "decorators.py"


def docstring_examples(source: str) -> dict[str, str]:
    """Decorator name to the dedented source of its docstring's `Example:` block."""
    out = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        doc = ast.get_docstring(node) or ""
        if "Example:" in doc:
            out[node.name] = textwrap.dedent(doc.split("Example:", 1)[1].split("\n", 1)[1])
    return out


def _stubbed(example: str) -> ast.Module:
    tree = ast.parse(example)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            node.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
    return tree


def refusal(example: str, jetstream: bool | None = None) -> str | None:
    """What discovery says about a service declaring this example, or None.

    JetStream is on exactly when the example declares a durable, unless forced.
    """
    tree = _stubbed(example)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    namespace: dict = {
        name: getattr(cliffracer, name) for name in names if hasattr(cliffracer, name)
    }
    for name in names - set(namespace) - set(dir(builtins)):
        namespace[name] = type(name, (BaseModel,), {})
    body = textwrap.indent(ast.unparse(tree), "    ")
    source = f"class Example(CliffracerService):\n{body}\n"
    namespace["CliffracerService"] = CliffracerService
    exec(compile(source, "<docstring example>", "exec"), namespace)  # noqa: S102
    enabled = "durable=" in example if jetstream is None else jetstream
    config = ServiceConfig(name="example", jetstream_enabled=enabled)
    try:
        namespace["Example"](config)._discover_handlers()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def test_the_reader_finds_the_examples():
    """No examples found and every example accepted look alike."""
    found = docstring_examples(DECORATORS.read_text())

    assert {"listener", "validated_listener", "broadcast", "timer"} <= set(found), sorted(found)


@pytest.mark.parametrize("decorator", sorted(docstring_examples(DECORATORS.read_text())))
def test_the_docstring_example_declares_handlers_a_service_accepts(decorator: str):
    example = docstring_examples(DECORATORS.read_text())[decorator]

    assert refusal(example) is None, f"@{decorator}'s docstring example: {refusal(example)}"


def test_CONTROL_an_untyped_event_handler_example_is_refused():
    example = (
        '@listener("user.events.*", fanout=True)\n'
        "async def handle_user_event(self, subject: str, **data):\n"
        "    ...\n"
    )

    found = refusal(example)
    assert found is not None and "UntypedHandler" in found, found


def test_CONTROL_a_durable_example_is_judged_with_jetstream_on():
    """The reader enables JetStream for an example declaring a durable, so the
    durable is not refused for a reason the example never claimed."""
    example = (
        '@listener("events.extraction.requested", durable="pdf-extractor")\n'
        "async def on_request(self, subject: str, document_id: str) -> None:\n"
        "    ...\n"
    )

    assert refusal(example) is None
    refused = refusal(example, jetstream=False)
    assert refused is not None and "jetstream_enabled=False" in refused, refused

"""Tests verifying documented RPC and event handlers have valid annotations and can start."""

import ast
import asyncio
import inspect
import re
from pathlib import Path

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.typed_rpc import build_handler_spec
from tests.unit.test_docs_code_blocks_resolve import REPO, _rel, docs, fences

pytestmark = pytest.mark.unit

# The decorators whose handlers are typed from their annotations. `@listener`
# and `@broadcast` take a subject and are checked elsewhere; `@timer` and
# `@cron` take no message.
TYPED_DECORATORS = ("rpc", "async_rpc")


def _is_typed_handler(node: ast.stmt) -> bool:
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    for dec in node.decorator_list:
        # `@rpc`, `@cliffracer.rpc`, and the factory forms `@rpc(...)`.
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = getattr(target, "attr", None) or getattr(target, "id", None)
        if name in TYPED_DECORATORS:
            return True
    return False


def _stub(node: ast.AsyncFunctionDef | ast.FunctionDef):
    """The handler alone, with its body replaced, compiled in an empty module.

    The BODY is dropped on purpose: it may call anything, and this guard is
    about the signature. The annotations are evaluated in a namespace built
    from the block's own imports and class definitions, so a documented model
    is resolved rather than stringified -- a stringified annotation would pass
    this guard while failing for the reader, which is the failure mode it
    exists to prevent.
    """
    stub = ast.AsyncFunctionDef(
        name=node.name,
        args=node.args,
        body=[ast.Expr(value=ast.Constant(value=Ellipsis))],
        decorator_list=[],
        returns=node.returns,
        type_params=[],
    )
    return ast.fix_missing_locations(stub)


def handlers_that_cannot_start(paths=None) -> list[str]:
    """One line per documented handler that `build_handler_spec` refuses."""
    out = []
    for doc in paths if paths is not None else docs():
        for start, source in fences(doc):
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue  # the guard next door reports these
            # Everything the block defines or imports, so annotations resolve.
            namespace: dict = {}
            preamble = [
                n
                for n in tree.body
                if isinstance(n, ast.Import | ast.ImportFrom | ast.ClassDef | ast.Assign)
            ]
            # Execute preamble statements individually to resolve definitions resiliently.
            for statement in preamble:
                try:
                    exec(  # noqa: S102 - documentation we control, to resolve its own names
                        compile(ast.Module(body=[statement], type_ignores=[]), "<doc>", "exec"),
                        namespace,
                    )
                except Exception:
                    continue  # an unresolvable import is the other guard's finding

            for node in ast.walk(tree):
                if not _is_typed_handler(node):
                    continue
                line = start + node.lineno
                local = dict(namespace)
                try:
                    exec(  # noqa: S102
                        compile(ast.Module(body=[_stub(node)], type_ignores=[]), "<doc>", "exec"),
                        local,
                    )
                except Exception as exc:  # a signature Python itself rejects
                    out.append(f"{_rel(doc)}:{line} {node.name}: {type(exc).__name__}: {exc}")
                    continue
                try:
                    build_handler_spec(node.name, local[node.name], owner=type("Doc", (), {}))
                except Exception as exc:
                    out.append(f"{_rel(doc)}:{line} @rpc {node.name}: {exc}")
    return out


def documented_handlers(paths=None) -> list[str]:
    """Every typed handler the extractor finds, so a silent zero is visible."""
    out = []
    for doc in paths if paths is not None else docs():
        for start, source in fences(doc):
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            out += [
                f"{_rel(doc)}:{start + n.lineno} {n.name}"
                for n in ast.walk(tree)
                if _is_typed_handler(n)
            ]
    return out


def test_the_extractor_finds_the_documented_handlers():
    """ "0 cannot start" is what a clean tree and a broken extractor look like alike."""
    found = documented_handlers()
    assert len(found) >= 15, f"only found {len(found)} documented @rpc handlers: {found}"


def test_every_documented_rpc_handler_would_start():
    broken = handlers_that_cannot_start()
    assert not broken, (
        "these documented handlers make a service refuse to start; a reader who "
        "copies one gets UntypedHandler from start():\n  " + "\n  ".join(broken)
    )


def test_CONTROL_a_bare_dict_return_is_detected(tmp_path: Path):
    """The exact shape that was in nineteen places, caught."""
    doc = tmp_path / "d.md"
    doc.write_text(
        "```python\nfrom cliffracer import CliffracerService, rpc\n\n\n"
        "class S(CliffracerService):\n    @rpc\n"
        "    async def go(self) -> dict:\n        return {}\n```\n"
    )
    found = handlers_that_cannot_start([doc])
    assert len(found) == 1, found
    assert "go" in found[0] and "dict" in found[0], found


def test_CONTROL_a_parameterised_annotation_passes(tmp_path: Path):
    """The other half, and the reason the rule is asked of the code.

    `dict[str, int]` is a contract; a bare `dict` is not. A guard that banned
    the word `dict` would red on correct documentation, and a guard that reds
    on correct code teaches the next author to weaken it.
    """
    doc = tmp_path / "d.md"
    doc.write_text(
        "```python\nfrom cliffracer import CliffracerService, rpc\n\n\n"
        "class S(CliffracerService):\n    @rpc\n"
        "    async def go(self, n: int) -> dict[str, int]:\n        return {}\n```\n"
    )
    assert handlers_that_cannot_start([doc]) == []


def test_CONTROL_a_documented_model_resolves(tmp_path: Path):
    """A block's own class must be usable as an annotation.

    If the namespace were not built from the block, every pydantic model in the
    documentation would read as an unresolvable name and this guard would red
    on the examples it most wants to keep.
    """
    doc = tmp_path / "d.md"
    doc.write_text(
        "```python\nfrom pydantic import BaseModel\n\n"
        "from cliffracer import CliffracerService, rpc\n\n\n"
        "class Order(BaseModel):\n    sku: str\n\n\n"
        "class S(CliffracerService):\n    @rpc\n"
        "    async def go(self, order: Order) -> Order:\n        return order\n```\n"
    )
    assert handlers_that_cannot_start([doc]) == []


def test_CONTROL_a_missing_return_annotation_is_detected(tmp_path: Path):
    """Verify that missing return annotations on RPC handlers are detected."""
    doc = tmp_path / "d.md"
    doc.write_text(
        "```python\nfrom cliffracer import CliffracerService, rpc\n\n\n"
        "class S(CliffracerService):\n    @rpc\n"
        "    async def go(self, n: int):\n        return {}\n```\n"
    )
    assert len(handlers_that_cannot_start([doc])) == 1


# --- Documented secret key validation ---

_SECRET_KEY = re.compile(r'secret_key=(?:"([^"]*)"|\'([^\']*)\')')


def _required_secret_length() -> int:
    """Return minimum secret length enforced by SimpleAuthService."""
    source = (
        REPO / "packages" / "cliffracer-auth" / "src" / "cliffracer_auth" / "simple_auth.py"
    ).read_text()
    found = re.search(r"len\(config\.secret_key\)\s*<\s*(\d+)", source)
    assert found, "the secret-key length check moved; this guard cannot read it any more"
    return int(found.group(1))


def short_secret_keys(paths=None) -> list[str]:
    minimum = _required_secret_length()
    out = []
    for doc in paths if paths is not None else docs():
        for start, source in fences(doc):
            for i, line in enumerate(source.splitlines(), 1):
                m = _SECRET_KEY.search(line)
                if not m:
                    continue
                value = m.group(1) if m.group(1) is not None else m.group(2)
                if len(value) < minimum:
                    out.append(
                        f"{_rel(doc)}:{start + i} secret_key is {len(value)} chars, "
                        f"needs {minimum}: {value!r}"
                    )
    return out


def test_every_documented_secret_key_is_long_enough():
    short = short_secret_keys()
    assert not short, (
        "SimpleAuthService raises ValueError on these, so the example fails on "
        "its first line:\n  " + "\n  ".join(short)
    )


def test_CONTROL_a_short_secret_key_is_detected(tmp_path: Path):
    """And the reader of the minimum still finds it."""
    assert _required_secret_length() == 32
    doc = tmp_path / "d.md"
    doc.write_text('```python\nauth = AuthConfig(secret_key="too-short")\n```\n')
    found = short_secret_keys([doc])
    assert len(found) == 1 and "9 chars" in found[0], found


def test_CONTROL_a_long_enough_secret_key_passes(tmp_path: Path):
    doc = tmp_path / "d.md"
    doc.write_text(
        '```python\nauth = AuthConfig(secret_key="a-secret-key-of-at-least-32-characters")\n```\n'
    )
    assert short_secret_keys([doc]) == []


def documented_listeners_without_declaration(paths=None) -> list[str]:
    """Every @listener or @validated_listener missing fanout or durable."""
    out = []
    for doc in paths if paths is not None else docs():
        for start, source in fences(doc):
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name in ("listener", "validated_listener"):
                    kwargs = {kw.arg for kw in node.keywords if kw.arg is not None}
                    if not ({"fanout", "durable"} & kwargs):
                        line = start + node.lineno
                        out.append(
                            f"{_rel(doc)}:{line} @{func_name} declares neither fanout nor durable"
                        )
    return out


def documented_services_that_cannot_start(paths=None) -> list[str]:
    """Instantiate and run _discover_handlers on all documented CliffracerService classes."""
    out = []
    for doc in paths if paths is not None else docs():
        for start, source in fences(doc):
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue

            has_service_subclass = any(isinstance(node, ast.ClassDef) for node in tree.body)
            if not has_service_subclass:
                continue

            safe_nodes = [
                n
                for n in tree.body
                if isinstance(n, ast.Import | ast.ImportFrom | ast.ClassDef | ast.Assign)
            ]
            namespace = {"__name__": "<doc>"}
            for node in safe_nodes:
                try:
                    exec(  # noqa: S102
                        compile(ast.Module(body=[node], type_ignores=[]), "<doc>", "exec"),
                        namespace,
                    )
                except Exception:
                    continue

            for name, obj in namespace.items():
                if (
                    isinstance(obj, type)
                    and issubclass(obj, CliffracerService)
                    and obj is not CliffracerService
                ):
                    try:
                        sig = inspect.signature(obj.__init__)
                        params = list(sig.parameters.values())[1:]
                        if not params or all(
                            p.default is not inspect.Parameter.empty for p in params
                        ):
                            instance = obj()
                        else:
                            instance = obj(ServiceConfig(name=f"test_{name.lower()}"))
                        if hasattr(instance, "container") and hasattr(
                            instance.container, "setup_extensions"
                        ):
                            asyncio.run(instance.container.setup_extensions())
                        elif hasattr(instance, "_setup_extensions"):
                            asyncio.run(instance._setup_extensions())
                        instance._discover_handlers()
                    except Exception as exc:
                        out.append(f"{_rel(doc)}:{start} {name}: {type(exc).__name__}: {exc}")
    return out


@pytest.mark.unit
def test_every_documented_listener_declares_fanout_or_durable():
    missing = documented_listeners_without_declaration()
    assert not missing, (
        "these documented listeners declare neither fanout=True nor durable=...:\n  "
        + "\n  ".join(missing)
    )


@pytest.mark.unit
def test_every_documented_service_subclass_would_start():
    broken = documented_services_that_cannot_start()
    assert not broken, (
        "these documented CliffracerService subclasses fail to discover handlers:\n  "
        + "\n  ".join(broken)
    )


@pytest.mark.unit
def test_CONTROL_a_listener_without_fanout_is_detected(tmp_path: Path):
    doc = tmp_path / "d.md"
    doc.write_text(
        "```python\nfrom cliffracer import listener\n\n"
        "@listener('order.created')\nasync def on_order(self, item_id: str = ''): pass\n```\n"
    )
    found = documented_listeners_without_declaration([doc])
    assert len(found) == 1 and "declares neither fanout nor durable" in found[0]


@pytest.mark.unit
def test_CONTROL_a_service_with_undeclared_listener_is_detected(tmp_path: Path):
    doc = tmp_path / "d.md"
    doc.write_text(
        "```python\nfrom cliffracer import CliffracerService, ServiceConfig, listener\n\n"
        "class S(CliffracerService):\n"
        "    def __init__(self):\n"
        "        super().__init__(ServiceConfig(name='s'))\n"
        "    @listener('order.created')\n"
        "    async def on_order(self, item_id: str = ''): pass\n```\n"
    )
    found = documented_services_that_cannot_start([doc])
    assert len(found) == 1 and "ConfigurationError" in found[0]

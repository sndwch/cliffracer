"""Tests verifying documented RPC and event handlers have valid annotations and can start."""

import ast
import asyncio
import contextlib
import contextvars
import inspect
import re
import typing
from pathlib import Path

import pytest

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    broadcast,
    listener,
    rpc,
    timer,
)
from cliffracer.core.typed_events import build_event_spec
from cliffracer.core.typed_rpc import build_handler_spec
from tests.repo.test_docs_code_blocks_resolve import REPO, _rel, docs, fences, markdown_line

pytestmark = pytest.mark.repo


# The decorators whose handlers are typed from their annotations, and the spec
# builder each is refused by at startup. `@timer` and `@cron` take no message.
TYPED_DECORATORS = {
    "rpc": build_handler_spec,
    "async_rpc": build_handler_spec,
    "listener": build_event_spec,
    "validated_listener": build_event_spec,
    "broadcast": build_event_spec,
}


def _typed_decorator(node: ast.stmt) -> str | None:
    """The typed decorator on this function, or None."""
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return None
    for dec in node.decorator_list:
        # `@rpc`, `@cliffracer.rpc`, and the factory forms `@rpc(...)`.
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = getattr(target, "attr", None) or getattr(target, "id", None)
        if name in TYPED_DECORATORS:
            return name
    return None


def _is_typed_handler(node: ast.stmt) -> bool:
    return _typed_decorator(node) is not None


def _yields(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """Whether the function's own body yields, which makes it a generator; a function, lambda or
    class nested in it does not count."""
    pending: list[ast.AST] = list(node.body)
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Yield | ast.YieldFrom):
            return True
        if not isinstance(
            current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
        ):
            pending.extend(ast.iter_child_nodes(current))
    return False


def _stub(node: ast.AsyncFunctionDef | ast.FunctionDef):
    """The handler alone, with its body replaced, compiled in an empty module.

    The BODY is dropped on purpose: it may call anything, and this guard is
    about the signature. The annotations are evaluated in a namespace built
    from the block's own imports and class definitions, so a documented model
    is resolved rather than stringified -- a stringified annotation would pass
    this guard while failing for the reader, which is the failure mode it
    exists to prevent. Whether the handler is a generator is part of what a
    start reads, so a body that yields leaves an unreached `yield` in the stub.
    """
    yields = _yields(node)
    body: list[ast.stmt] = [ast.Expr(value=ast.Constant(value=Ellipsis))]
    if yields:
        body.append(ast.If(test=ast.Constant(value=False), body=[ast.Expr(ast.Yield())], orelse=[]))
    stub = ast.AsyncFunctionDef(
        name=node.name,
        args=node.args,
        body=body,
        decorator_list=[],
        returns=node.returns,
        type_params=[],
    )
    return ast.fix_missing_locations(stub)


def handlers_that_cannot_start(paths=None) -> list[str]:
    """One line per documented handler that `build_handler_spec` refuses."""
    out = []
    for doc in paths if paths is not None else docs():
        for source_line, source in fences(doc):
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
                    # In a copy of the context: a documentation example may set the correlation
                    # id (`create_correlation_id()`), and that is the example's, not this test's.
                    contextvars.copy_context().run(
                        exec,  # noqa: S102 - documentation we control, to resolve its own names
                        compile(ast.Module(body=[statement], type_ignores=[]), "<doc>", "exec"),
                        namespace,
                    )
                except Exception:
                    continue  # an unresolvable import is the other guard's finding

            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if not _is_typed_handler(node):
                    continue
                line = markdown_line(source_line, node.lineno)
                local = dict(namespace)
                try:
                    exec(  # noqa: S102
                        compile(ast.Module(body=[_stub(node)], type_ignores=[]), "<doc>", "exec"),
                        local,
                    )
                except Exception as exc:  # a signature Python itself rejects
                    out.append(f"{_rel(doc)}:{line} {node.name}: {type(exc).__name__}: {exc}")
                    continue
                decorator = _typed_decorator(node)
                assert decorator is not None
                try:
                    TYPED_DECORATORS[decorator](
                        node.name, local[node.name], owner=type("Doc", (), {})
                    )
                except Exception as exc:
                    out.append(f"{_rel(doc)}:{line} @{decorator} {node.name}: {exc}")
    return out


def documented_handlers(paths=None) -> list[str]:
    """Every typed handler the extractor finds, so a silent zero is visible."""
    out = []
    for doc in paths if paths is not None else docs():
        for source_line, source in fences(doc):
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            out += [
                f"{_rel(doc)}:{markdown_line(source_line, n.lineno)} {n.name}"
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and _is_typed_handler(n)
            ]
    return out


def test_the_extractor_finds_the_documented_handlers():
    """ "0 cannot start" is what a clean tree and a broken extractor look like alike."""
    found = documented_handlers()
    # Within a fifth of what the documentation holds (56 when this was set): a floor far below
    # the count lets a docs directory leave the glob, or a fence style change, and stay green.
    assert len(found) >= 45, f"only found {len(found)} documented typed handlers: {found}"


def test_CONTROL_an_async_rpc_handler_is_found_and_refused_like_an_rpc_one(tmp_path: Path):
    """No documented example uses `@async_rpc`, so nothing else shows its arm of the table works.

    A document is written for the purpose: one handler a startup would refuse (a parameter with no
    annotation) and one it would accept. The arm finds both and refuses only the first.
    """
    doc = tmp_path / "async_rpc.md"
    doc.write_text(
        "```python\n"
        "from cliffracer import CliffracerService, async_rpc\n\n\n"
        "class Worker(CliffracerService):\n"
        "    @async_rpc\n"
        "    async def refused(self, item) -> None: ...\n\n"
        "    @async_rpc\n"
        "    async def accepted(self, item: str) -> None: ...\n"
        "```\n"
    )

    assert [h.split()[-1] for h in documented_handlers([doc])] == ["refused", "accepted"]
    problems = handlers_that_cannot_start([doc])
    assert len(problems) == 1 and "@async_rpc refused" in problems[0], problems


def test_CONTROL_a_generator_handler_is_judged_as_the_generator_it_is(tmp_path: Path):
    """The stub drops the body, and with it the `yield` that makes a handler a generator. A
    streaming handler is accepted only as an async generator, and a sync generator is refused."""
    doc = tmp_path / "generators.md"
    doc.write_text(
        "```python\n"
        "from collections.abc import AsyncIterator, Iterator\n\n"
        "from cliffracer import CliffracerService, rpc\n\n\n"
        "class Feed(CliffracerService):\n"
        "    @rpc\n"
        "    async def streams(self, n: int) -> AsyncIterator[int]:\n"
        "        def nested():\n"
        "            yield 0\n\n"
        "        for value in range(n):\n"
        "            yield value\n\n"
        "    @rpc\n"
        "    def sync_generator(self, n: int) -> Iterator[int]:\n"
        "        yield n\n\n"
        "    @rpc\n"
        "    async def only_nested(self, n: int) -> AsyncIterator[int]:\n"
        "        async def nested():\n"
        "            yield n\n\n"
        "        return nested()\n"
        "```\n"
    )

    problems = handlers_that_cannot_start([doc])
    assert [p.split(":")[1].split()[-1] for p in problems] == ["sync_generator", "only_nested"], (
        problems
    )


def test_the_extractor_finds_documented_event_handlers_too():
    """The RPC count alone would stay green if event handlers were never read."""
    events = [
        f"{_rel(doc)}:{markdown_line(source_line, n.lineno)} {n.name}"
        for doc in docs()
        for source_line, source in fences(doc)
        for n in ast.walk(_parsed(source))
        if _typed_decorator(n) in ("listener", "validated_listener", "broadcast")
    ]
    assert len(events) >= 5, f"only found {len(events)} documented event handlers: {events}"


def _parsed(source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError:
        return ast.Module(body=[], type_ignores=[])


def test_every_documented_typed_handler_would_start():
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
    expected = next(
        number
        for number, line in enumerate(doc.read_text().splitlines(), 1)
        if "async def go" in line
    )
    assert found[0].startswith(f"{doc}:{expected} ")


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


def test_CONTROL_a_listener_taking_kwargs_is_detected(tmp_path: Path):
    """The shape every event example used to have, as a free-standing function.

    Free-standing, not inside a service class, because that is how the snippets
    in the guides are written and the class-based check never sees them.
    """
    doc = tmp_path / "d.md"
    doc.write_text(
        "```python\nfrom cliffracer import listener\n\n\n"
        '@listener("user.created", fanout=True)\n'
        "async def on_user_created(self, subject: str, **data):\n    pass\n```\n"
    )
    found = handlers_that_cannot_start([doc])
    assert len(found) == 1, found
    assert "@listener on_user_created" in found[0] and "*data" in found[0], found


def test_CONTROL_a_typed_free_standing_listener_passes(tmp_path: Path):
    doc = tmp_path / "d.md"
    doc.write_text(
        "```python\nfrom cliffracer import broadcast, listener\n\n\n"
        '@listener("user.created", fanout=True)\n'
        "async def on_user_created(self, subject: str, user_id: str) -> None:\n    pass\n\n\n"
        '@broadcast("system.alerts")\n'
        "async def on_alert(self, subject: str, level: str) -> None:\n    pass\n```\n"
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
    found = re.search(r"len\(config\.secret_key(?:\.get_secret_value\(\))?\)\s*<\s*(\d+)", source)
    assert found, "the secret-key length check moved; this guard cannot read it any more"
    return int(found.group(1))


def short_secret_keys(paths=None) -> list[str]:
    minimum = _required_secret_length()
    out = []
    for doc in paths if paths is not None else docs():
        for source_line, source in fences(doc):
            for i, line in enumerate(source.splitlines(), 1):
                m = _SECRET_KEY.search(line)
                if not m:
                    continue
                value = m.group(1) if m.group(1) is not None else m.group(2)
                if len(value) < minimum:
                    out.append(
                        f"{_rel(doc)}:{markdown_line(source_line, i)} "
                        f"secret_key is {len(value)} chars, "
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
        for source_line, source in fences(doc):
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
                        line = markdown_line(source_line, node.lineno)
                        out.append(
                            f"{_rel(doc)}:{line} @{func_name} declares neither fanout nor durable"
                        )
    return out


# Keyed by the line the documented class is declared on, which is the location
# the failure reporter prints, so a key can be read straight off a failure.
# test_every_service_exemption_is_load_bearing fails if an edit moves a class
# past its key, rather than letting the exemption quietly point at nothing.
EXEMPT_SERVICE_EXAMPLES: dict[str, str] = {
    "QUICKSTART.md:209": "the documented wrong way to declare a handler, shown so a reader recognises it",
}


def _build_service_namespace() -> dict[str, typing.Any]:
    ns: dict[str, typing.Any] = {
        "__name__": "<doc>",
        "CliffracerService": CliffracerService,
        "ServiceConfig": ServiceConfig,
        "rpc": rpc,
        "listener": listener,
        "broadcast": broadcast,
        "timer": timer,
    }
    for modname in (
        "cliffracer",
        "cliffracer_metrics",
        "cliffracer_otel",
        "cliffracer_auth",
        "cliffracer_resilience",
        "cliffracer_cron",
        "cliffracer_kv",
    ):
        try:
            m = __import__(modname)
            for k in dir(m):
                if not k.startswith("_") and k != "config":
                    val = getattr(m, k)
                    if not inspect.ismodule(val):
                        ns[k] = val
        except ImportError:
            pass
    return ns


# The modules whose public surface a fence may use without the document
# showing the import. A reader has the library installed, so naming its
# exports is fair; naming anything else is a binding the guard handed the
# fence, which hides the NameError a reader would get.
NAMESPACE_SOURCES = (
    "cliffracer",
    "cliffracer_metrics",
    "cliffracer_otel",
    "cliffracer_auth",
    "cliffracer_resilience",
    "cliffracer_cron",
    "cliffracer_kv",
)


def _library_exports() -> set[str]:
    """Names a fence may use for free: the library's non-module public surface.

    Modules are excluded because `_build_service_namespace` excludes them, so
    counting `cliffracer_kv.config` as an export would admit a binding named
    `config` that the namespace never got from the library.
    """
    exported: set[str] = {"__name__"}
    for modname in NAMESPACE_SOURCES:
        try:
            module = __import__(modname)
        except ImportError:
            continue
        exported |= {
            k
            for k in dir(module)
            if not k.startswith("_") and not inspect.ismodule(getattr(module, k, None))
        }
    return exported


def test_the_namespace_hands_the_fences_nothing_the_library_does_not_export():
    """Every name a documented fence gets for free must come from the library.

    An entry that does not is a name the guard introduces on the document's
    behalf: the fence execs cleanly here and raises NameError for the reader
    who copies it. `SECRET` was one, and removing it exposed a live defect
    immediately; `config`, `datetime` and pydantic's exports were three more.
    """
    handed_out = sorted(set(_build_service_namespace()) - _library_exports())
    assert handed_out == [], (
        "these names are handed to every documented fence but are not exported "
        f"by the library, so a document may use them without showing where they "
        f"come from: {handed_out}"
    )


def test_CONTROL_an_injected_name_is_detected():
    """The check above must be able to see an injection."""
    polluted = dict(_build_service_namespace())
    polluted["ZQX_HANDED_OUT"] = object()
    assert sorted(set(polluted) - _library_exports()) == ["ZQX_HANDED_OUT"]


def test_CONTROL_a_submodule_name_is_not_a_library_export():
    """`cliffracer_cyanide.config` and `cliffracer_kv.config` are modules, and the
    namespace builder skips modules. Counting them as exports would let a
    binding named `config` pass as if the library provided it."""
    assert "config" not in _library_exports()


def _base_names(node: ast.ClassDef) -> set[str]:
    """The names a class lists as bases, however they are spelled."""
    names: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def fence_service_classes(tree: ast.Module, source_line: int, known: set[str]) -> dict[str, int]:
    """Documented service classes in one fence, keyed to the line each is on.

    A class counts when it names CliffracerService directly, or when it names a
    class already established as one -- a fence that declares an intermediate
    base and subclasses it is the shape a syntactic base check cannot see, and
    a documented service invisible to the walk is invisible to the guard too.

    `known` carries the names established by earlier fences in the same
    document, and is updated in place so a later fence can build on them.
    """
    found: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = _base_names(node)
        if "CliffracerService" in bases or (bases & known):
            known.add(node.name)
            found[node.name] = markdown_line(source_line, node.lineno)
    return found


def documented_services_that_cannot_start(paths=None) -> list[str]:
    """Instantiate and run _discover_handlers on all documented CliffracerService classes."""
    out = []
    for doc in paths if paths is not None else docs():
        doc_ns = _build_service_namespace()
        service_names: set[str] = set()
        for source_line, source in fences(doc):
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue

            fence_classes = fence_service_classes(tree, source_line, service_names)

            executable_nodes = [
                n
                for n in tree.body
                if isinstance(
                    n,
                    ast.Import
                    | ast.ImportFrom
                    | ast.ClassDef
                    | ast.Assign
                    | ast.FunctionDef
                    | ast.AsyncFunctionDef,
                )
            ]
            fence_ns = dict(doc_ns)
            for node in executable_nodes:
                node_line = markdown_line(source_line, node.lineno)
                loc = f"{_rel(doc)}:{node_line}"
                if loc in EXEMPT_SERVICE_EXAMPLES:
                    continue
                try:
                    # In a copy of the context, for the reason given where the preamble is run.
                    contextvars.copy_context().run(
                        exec,  # noqa: S102
                        compile(ast.Module(body=[node], type_ignores=[]), "<doc>", "exec"),
                        fence_ns,
                    )
                    doc_ns.update({k: v for k, v in fence_ns.items() if not k.startswith("__")})
                except Exception as exc:
                    if isinstance(node, ast.ClassDef) and node.name in fence_classes:
                        out.append(
                            f"{_rel(doc)}:{node_line} {node.name}: {type(exc).__name__}: {exc}"
                        )

            if not fence_classes:
                continue

            for name, class_line in fence_classes.items():
                loc = f"{_rel(doc)}:{class_line}"
                if loc in EXEMPT_SERVICE_EXAMPLES:
                    continue
                obj = fence_ns.get(name)
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
                            instance = obj()  # type: ignore[call-arg]
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
                        out.append(f"{_rel(doc)}:{class_line} {name}: {type(exc).__name__}: {exc}")
    return out


@contextlib.contextmanager
def _service_exemptions(mapping):
    """Run the guard with a different exemption set, then restore it."""
    global EXEMPT_SERVICE_EXAMPLES
    original = EXEMPT_SERVICE_EXAMPLES
    EXEMPT_SERVICE_EXAMPLES = mapping
    try:
        yield
    finally:
        EXEMPT_SERVICE_EXAMPLES = original


def test_every_documented_listener_declares_fanout_or_durable():
    missing = documented_listeners_without_declaration()
    assert not missing, (
        "these documented listeners declare neither fanout=True nor durable=...:\n  "
        + "\n  ".join(missing)
    )


def test_every_service_exemption_is_load_bearing():
    """Each exemption must name a location the guard would otherwise report.

    The keys carry a line number, so an ordinary documentation edit can move a
    class past its key. Without this, the exemption stops matching and the
    guard starts failing on an example that was meant to be exempt, or keeps
    matching a line that now holds something else.
    """
    dead = []
    for loc in EXEMPT_SERVICE_EXAMPLES:
        without = {k: v for k, v in EXEMPT_SERVICE_EXAMPLES.items() if k != loc}
        with _service_exemptions(without):
            reported = {f.split(" ", 1)[0] for f in documented_services_that_cannot_start()}
        if loc not in reported:
            dead.append(loc)
    assert dead == [], (
        "these exemptions name a location the guard does not report; the "
        f"documentation moved or was fixed under them: {dead}"
    )


def test_the_service_exemptions_all_carry_a_reason():
    empty = [loc for loc, reason in EXEMPT_SERVICE_EXAMPLES.items() if not reason.strip()]
    assert empty == [], f"exemptions without a stated reason: {empty}"


def test_every_documented_service_subclass_would_start():
    broken = documented_services_that_cannot_start()
    assert not broken, (
        "these documented CliffracerService subclasses fail to discover handlers:\n  "
        + "\n  ".join(broken)
    )


def documented_service_locations(paths=None) -> list[str]:
    """Every documented service class the walk finds, as `path:line name`.

    The same walk the start-up guard uses, so the count below cannot drift from
    what that guard actually reads. Re-walking the fences here is how a floor
    comes to certify a number the real guard never produced.
    """
    found: list[str] = []
    for doc in paths if paths is not None else docs():
        service_names: set[str] = set()
        for source_line, source in fences(doc):
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for name, line in fence_service_classes(tree, source_line, service_names).items():
                found.append(f"{_rel(doc)}:{line} {name}")
    return found


def test_the_service_extractor_finds_the_documented_services():
    """Ensure documented CliffracerService classes are discovered across docs."""
    services = documented_service_locations()
    assert len(services) >= 49, f"only found {len(services)} service subclasses: {services}"


def test_CONTROL_a_service_subclassing_a_base_in_the_same_fence_is_found():
    """The shape a syntactic base check cannot see.

    `class Svc(Base)` where `Base` is declared in the same fence names
    CliffracerService nowhere, so a walk matching bases by name alone drops it
    -- silently, because a class the walk never collects is one the guard never
    reports on either.
    """
    source = (
        "from cliffracer import CliffracerService\n"
        "class Base(CliffracerService): pass\n"
        "class Svc(Base): pass\n"
    )
    tree = ast.parse(source)
    found = fence_service_classes(tree, 0, set())
    assert set(found) == {"Base", "Svc"}, found


def test_CONTROL_an_unrelated_class_is_not_collected():
    """And the walk does not simply collect every class it sees."""
    tree = ast.parse("class Plain: pass\nclass Other(Plain): pass\n")
    assert fence_service_classes(tree, 0, set()) == {}


def test_CONTROL_a_listener_without_fanout_is_detected(tmp_path: Path):
    doc = tmp_path / "d.md"
    doc.write_text(
        "```python\nfrom cliffracer import listener\n\n"
        "@listener('order.created')\nasync def on_order(self, item_id: str = ''): pass\n```\n"
    )
    found = documented_listeners_without_declaration([doc])
    assert len(found) == 1 and "declares neither fanout nor durable" in found[0]


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


def test_CONTROL_a_service_with_invalid_class_definition_is_detected(tmp_path: Path):
    """Control: an unresolvable attribute in a service class body is caught."""
    doc = tmp_path / "broken_service.md"
    doc.write_text(
        "```python\n"
        "from cliffracer import CliffracerService\n\n"
        "class BrokenService(CliffracerService):\n"
        "    tag = TOTALLY_UNDEFINED_ATTRIBUTE\n"
        "```\n"
    )
    broken = documented_services_that_cannot_start([doc])
    assert broken and any("TOTALLY_UNDEFINED_ATTRIBUTE" in b for b in broken)


@pytest.mark.parametrize("blank_lines", [0, 1, 2])
def test_service_failures_report_the_class_document_line(tmp_path: Path, blank_lines: int):
    doc = tmp_path / "broken_service.md"
    doc.write_text(
        "# Shipment service\n\n"
        "```python\n" + "\n" * blank_lines + "from cliffracer import CliffracerService\n\n"
        "class BrokenShipment(CliffracerService):\n"
        "    tag = MISSING_SHIPMENT_TAG\n"
        "```\n"
    )
    expected = next(
        number
        for number, line in enumerate(doc.read_text().splitlines(), 1)
        if line.startswith("class BrokenShipment")
    )
    broken = documented_services_that_cannot_start([doc])
    assert len(broken) == 1
    assert broken[0].startswith(f"{doc}:{expected} BrokenShipment: NameError:")


def test_services_in_multiple_fences_keep_their_own_document_lines(tmp_path: Path):
    doc = tmp_path / "services.md"
    doc.write_text(
        "```python\nfrom cliffracer import CliffracerService\n"
        "class Orders(CliffracerService): pass\n```\n\n"
        "```python\n\nclass Shipments(CliffracerService): pass\n```\n"
    )
    assert documented_service_locations([doc]) == [
        f"{doc}:3 Orders",
        f"{doc}:8 Shipments",
    ]

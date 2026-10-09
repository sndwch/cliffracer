"""A static call-graph guard over the dispatcher source.

Every callback the container hands to nats-py is followed, through the dispatcher
classes, to ExtensionPipeline.run_worker. The container subscribes its RPC and
describe subjects itself, and each event listener through ListenerSubscriptions,
which holds the listener subscriptions a `pause_when_down` pause stops and starts,
so both classes are read -- the method that actually runs the
extension worker hooks. This reads the shape of the source; it does not execute
anything. The runtime behaviour of the hook chain is covered by
tests/unit/test_worker_hooks.py, which is what reddens if the pipeline is
emptied while its call sites stay in place.
"""

import ast
import inspect
from collections.abc import Mapping
from pathlib import Path

import pytest

from cliffracer.core import dispatcher as dispatcher_module

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# The bind and provision paths hand the same JetStream entry point to two
# different nats-py methods, so there is one more binding than unique entry
# points.
_EXPECTED_CALLBACK_ENTRY_POINT_COUNT = 5
_EXPECTED_CALLBACK_BINDING_COUNT = 6


#: The classes that hand callbacks to nats-py, by the module that defines each.
SUBSCRIBING_CLASSES = (
    ("container.py", "Container"),
    ("listener_subscriptions.py", "ListenerSubscriptions"),
)


def _class_methods_ast(
    module: str, class_name: str
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Parse one class's methods directly from repository source AST."""
    path = REPO / "src" / "cliffracer" / "core" / module
    tree = ast.parse(path.read_text(), filename=str(path))
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    methods[item.name] = item
    return methods


def _container_methods_ast() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every method of the subscribing classes, keyed `Class.method`."""
    return {
        f"{class_name}.{name}": node
        for module, class_name in SUBSCRIBING_CLASSES
        for name, node in _class_methods_ast(module, class_name).items()
    }


def _nats_callback_bindings(
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] | str | None = None,
) -> list[tuple[str, str, str]]:
    """Return (method_name, kwarg, target) for every callback argument across Container methods."""
    if isinstance(methods, str):
        tree = ast.parse(methods)
        class_node = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        methods = {
            m.name: m
            for m in class_node.body
            if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
        }
    elif methods is None:
        methods = _container_methods_ast()

    bindings: list[tuple[str, str, str]] = []
    for method_name, method_node in methods.items():
        aliases = _local_aliases(method_node)
        for node in ast.walk(method_node):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg and (kw.arg == "cb" or kw.arg.endswith("cb")):
                    val = kw.value
                    if isinstance(val, ast.Attribute):
                        target = _resolved(ast.unparse(val), aliases)
                        bindings.append((method_name, kw.arg, target))
                    elif isinstance(val, ast.Call):
                        target = _resolved(ast.unparse(val.func), aliases)
                        bindings.append((method_name, kw.arg, target))
    return bindings


#: What a binding's object may be, written as the class that holds the dispatcher writes it:
#: the container's own, or ListenerSubscriptions' container's.
_DISPATCHER_SPELLINGS = ("self.dispatcher", "self._c.dispatcher")


def _local_aliases(method_node: ast.AST) -> dict[str, str]:
    """Each local name a method assigns once from an expression, to that expression's source."""
    seen: dict[str, list[str]] = {}
    for node in ast.walk(method_node):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                seen.setdefault(target.id, []).append(ast.unparse(node.value))
    return {name: values[0] for name, values in seen.items() if len(values) == 1}


def _resolved(target: str, aliases: dict[str, str]) -> str:
    """The target with a local alias replaced by what it was assigned, and the dispatcher's
    spellings written as `self.dispatcher`, so a local only passes for what it is bound to."""
    head, dot, rest = target.partition(".")
    if dot and head in aliases:
        target = f"{aliases[head]}.{rest}"
    for spelling in _DISPATCHER_SPELLINGS:
        if target.startswith(spelling + "."):
            return "self.dispatcher." + target[len(spelling) + 1 :]
    return target


def _callbacks_bound_to_nats(
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] | str | None = None,
) -> set[str]:
    return {
        target.split(".")[-1]
        for _, kwarg, target in _nats_callback_bindings(methods)
        if kwarg == "cb"
    }


# The method that runs the extension worker hooks. Reaching it is what the
# callbacks are being followed to.
HOOK_PIPELINE = ("ExtensionPipeline", "run_worker")

# The dispatcher object Container binds its callbacks to.
ENTRY_CLASS = "MessageDispatcher"


def _dispatcher_files() -> list[Path]:
    dispatcher_file = Path(inspect.getfile(dispatcher_module))
    files = [dispatcher_file]
    dispatch_dir = dispatcher_file.parent / "dispatch"
    if dispatch_dir.is_dir():
        files.extend(sorted(dispatch_dir.glob("*.py")))
    return files


def _dispatcher_methods() -> dict[tuple[str, str], ast.AST]:
    """Map (class name, method name) to its definition.

    Keyed by the pair, not the bare method name: `_run_worker`, `on_rpc_request`
    and `handle_event` are each defined in more than one dispatcher class, and a
    flat dict silently resolves to whichever file was parsed last.
    """
    methods: dict[tuple[str, str], ast.AST] = {}
    for path in _dispatcher_files():
        tree = ast.parse(path.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                for item in n.body:
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                        methods[(n.name, item.name)] = item
    return methods


def _annotation_class(node: ast.AST | None) -> str | None:
    """Return the class name an annotation names, looking through `X | None`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_class(node.left) or _annotation_class(node.right)
    return None


def _attribute_classes() -> dict[tuple[str, str], str]:
    """Map (class name, attribute) to the class assigned to it in __init__.

    Two spellings carry a type. `self.rpc = RpcDispatcher(...)` constructs one,
    and `self.pipeline = pipeline` stores a constructor parameter whose
    annotation names one. Both are needed: the collaborators are built in
    MessageDispatcher and handed down to the dispatchers that use them.
    """
    owners: dict[tuple[str, str], str] = {}
    for path in _dispatcher_files():
        tree = ast.parse(path.read_text())
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for item in cls.body:
                if not isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if item.name != "__init__":
                    continue
                annotated = {
                    a.arg: _annotation_class(a.annotation)
                    for a in item.args.args + item.args.kwonlyargs
                    if a.annotation is not None
                }
                for node in ast.walk(item):
                    if not isinstance(node, ast.Assign):
                        continue
                    made = None
                    if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                        made = node.value.func.id
                    elif isinstance(node.value, ast.Name):
                        made = annotated.get(node.value.id)
                    if made is None:
                        continue
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            owners[(cls.name, target.attr)] = made
    return owners


def _reachable_nodes(node: ast.AST):
    """Walk `node`, skipping branches a constant test makes unreachable.

    A call parked under `if False:` never runs, so it is not evidence that
    anything dispatches through anywhere.
    """
    if isinstance(node, ast.If):
        test = node.test
        if isinstance(test, ast.Constant):
            body = node.body if test.value else node.orelse
            for child in body:
                yield from _reachable_nodes(child)
            return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _reachable_nodes(child)


def _callee(
    call: ast.Call, owner: str, attribute_classes: Mapping[tuple[str, str], str]
) -> tuple[str, str] | None:
    """Resolve a call inside class `owner` to the (class, method) it invokes.

    `self.x()` is a method of the same class. `self.attr.x()` is a method of
    whatever class `attr` was assigned in __init__. Anything else -- a call on a
    parameter, a local, or an object this walk cannot type -- is unresolved, and
    an unresolved call is not evidence of reaching anything.
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    receiver = func.value
    if isinstance(receiver, ast.Name) and receiver.id == "self":
        return (owner, func.attr)
    if (
        isinstance(receiver, ast.Attribute)
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id == "self"
    ):
        held = attribute_classes.get((owner, receiver.attr))
        if held is not None:
            return (held, func.attr)
    return None


def _reaches(
    start: tuple[str, str],
    target: tuple[str, str],
    methods: Mapping[tuple[str, str], ast.AST],
    attribute_classes: Mapping[tuple[str, str], str],
    seen: set[tuple[str, str]] | None = None,
) -> bool:
    """Whether `start` invokes `target`, directly or through dispatcher methods.

    Requires a genuine ast.Call, on a receiver this walk can resolve, in a
    branch that can execute. A bare attribute reference, a call on some other
    object that happens to share the method name, and a call under a constant
    false test are all rejected.
    """
    seen = set() if seen is None else seen
    if start in seen or start not in methods:
        return False
    seen.add(start)
    owner = start[0]
    for child in _reachable_nodes(methods[start]):
        if not isinstance(child, ast.Call):
            continue
        callee = _callee(child, owner, attribute_classes)
        if callee is None:
            continue
        if callee == target:
            return True
        if _reaches(callee, target, methods, attribute_classes, seen):
            return True
    return False


def test_the_callback_scan_still_finds_callbacks():
    """Guard the guard: prove the extraction works before trusting it."""
    found = _callbacks_bound_to_nats()

    assert len(found) == _EXPECTED_CALLBACK_ENTRY_POINT_COUNT, (
        f"expected {_EXPECTED_CALLBACK_ENTRY_POINT_COUNT} `cb=` callback entry points across all "
        f"Container methods, found {sorted(found)}. If a callback was added or "
        "removed deliberately, update _EXPECTED_CALLBACK_COUNT; if this dropped to "
        "zero, the AST walk stopped matching and every other test here is vacuous."
    )


def test_the_reachability_scan_can_tell_reached_from_unreached():
    """Guard the guard, second half: a walk that returns True for everything --
    or False for everything -- satisfies the assertion below either way."""
    methods = _dispatcher_methods()
    attribute_classes = _attribute_classes()
    assert HOOK_PIPELINE in methods, "the target itself is gone; this file is now vacuous"

    assert _reaches((ENTRY_CLASS, "handle_event"), HOOK_PIPELINE, methods, attribute_classes), (
        "positive control: handle_event is known to dispatch through the chain"
    )
    # NEGATIVE control, and it must be a REAL method rather than an invented
    # name: a missing name returns False from the first line of _reaches, which
    # would pass this assertion without the walk doing anything at all.
    assert (ENTRY_CLASS, "report_consumer_drift") in methods
    assert not _reaches(
        (ENTRY_CLASS, "report_consumer_drift"), HOOK_PIPELINE, methods, attribute_classes
    ), (
        "negative control: report_consumer_drift is not a dispatch path, so a scan "
        "that reports it as reaching the pipeline is reporting True for everything"
    )


def test_every_nats_callback_dispatches_through_the_hook_chain():
    """Follow each callback from the class Container binds it on to the pipeline."""
    methods = _dispatcher_methods()
    attribute_classes = _attribute_classes()
    target_class, target_method = HOOK_PIPELINE
    uninstrumented = sorted(
        cb
        for cb in _callbacks_bound_to_nats()
        if not _reaches((ENTRY_CLASS, cb), HOOK_PIPELINE, methods, attribute_classes)
    )

    assert not uninstrumented, (
        f"{uninstrumented} are handed to NATS as callbacks but no call path from "
        f"{ENTRY_CLASS} reaches {target_class}.{target_method}, so their messages "
        "run no extension worker hooks."
    )


def test_every_nats_callback_binds_to_dispatcher():
    """Verify entry points handed to nats-py resolve directly to dispatcher instance."""
    bindings = _nats_callback_bindings()

    assert len(bindings) == _EXPECTED_CALLBACK_BINDING_COUNT, (
        f"expected {_EXPECTED_CALLBACK_BINDING_COUNT} nats-py callback bindings across Container methods, "
        f"found {bindings}. If this dropped, the AST visitor "
        "stopped matching and this test is now vacuous."
    )

    not_on_dispatcher = [(m, k, t) for m, k, t in bindings if not t.startswith("self.dispatcher.")]
    assert not not_on_dispatcher, (
        f"these callbacks do not bind directly to dispatcher: {not_on_dispatcher}. "
        "Callbacks must bind directly to self.dispatcher.<method> to avoid service bounce."
    )


def test_callback_scan_inspects_full_container_method_set():
    """The AST walk sees every method the imported classes define.

    Checked against the loaded classes rather than a hand-set floor: a threshold
    is a number nobody can source, and lowering it is the easy way to make a
    narrowed scan pass.
    """
    from cliffracer.core.container import Container
    from cliffracer.core.listener_subscriptions import ListenerSubscriptions

    def _defines_a_function(value: object) -> bool:
        """True for anything the class body wrote with a `def`, decorated or not."""
        if isinstance(value, property):
            return value.fget is not None
        if isinstance(value, staticmethod | classmethod):
            return True
        return inspect.isfunction(value)

    runtime_methods = {
        f"{cls.__name__}.{name}"
        for cls in (Container, ListenerSubscriptions)
        for name, value in vars(cls).items()
        if _defines_a_function(value)
    }
    parsed = _container_methods_ast()

    assert set(parsed) == runtime_methods, (
        "the AST walk and the imported classes disagree about their methods; "
        f"only parsed: {sorted(set(parsed) - runtime_methods)}, "
        f"only imported: {sorted(runtime_methods - set(parsed))}"
    )

    bindings = _nats_callback_bindings(parsed)
    assert len(bindings) == _EXPECTED_CALLBACK_BINDING_COUNT, (
        f"expected {_EXPECTED_CALLBACK_BINDING_COUNT} callback bindings, found {len(bindings)}"
    )


def test_CONTROL_callback_in_arbitrary_method_is_detected():
    """Control: an AST callback binding in a non-setup method is extracted."""
    sample_code = (
        "class Container:\n"
        "    async def _custom_health_worker(self):\n"
        "        await self.nc.subscribe('health.>', cb=self.dispatcher.on_health_check)\n"
    )
    tree = ast.parse(sample_code)
    class_node = tree.body[0]
    assert isinstance(class_node, ast.ClassDef)
    methods = {
        m.name: m for m in class_node.body if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    extracted = _nats_callback_bindings(methods)
    assert len(extracted) == 1
    method_name, kw, target = extracted[0]
    assert method_name == "_custom_health_worker"
    assert kw == "cb"
    assert target == "self.dispatcher.on_health_check"


def test_CONTROL_bare_attribute_reference_does_not_satisfy_reachability():
    """Control: referencing _run_worker as an attribute without calling it returns False."""
    sample_code = (
        "class MessageDispatcher:\n"
        "    async def bypassed_handler(self, ctx, call):\n"
        "        _ref = self._run_worker\n"
        "        await call()\n"
        "    async def _run_worker(self, ctx, call):\n"
        "        pass\n"
    )
    tree = ast.parse(sample_code)
    class_node = tree.body[0]
    assert isinstance(class_node, ast.ClassDef)
    methods = {
        (class_node.name, m.name): m
        for m in class_node.body
        if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert not _reaches(
        ("MessageDispatcher", "bypassed_handler"),
        ("MessageDispatcher", "_run_worker"),
        methods,
        {},
    ), "bare attribute reference must not satisfy reachability; an ast.Call is required"


def test_CONTROL_genuine_call_satisfies_reachability():
    """Control: an actual ast.Call node invoking _run_worker returns True."""
    sample_code = (
        "class MessageDispatcher:\n"
        "    async def active_handler(self, ctx, call):\n"
        "        await self._run_worker(ctx, call)\n"
        "    async def _run_worker(self, ctx, call):\n"
        "        pass\n"
    )
    tree = ast.parse(sample_code)
    class_node = tree.body[0]
    assert isinstance(class_node, ast.ClassDef)
    methods = {
        (class_node.name, m.name): m
        for m in class_node.body
        if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert _reaches(
        ("MessageDispatcher", "active_handler"),
        ("MessageDispatcher", "_run_worker"),
        methods,
        {},
    ), "genuine ast.Call node must satisfy reachability"


def _parse_classes(code: str) -> tuple[dict[tuple[str, str], ast.AST], dict[tuple[str, str], str]]:
    """Build the (class, method) map and attribute types for a sample module."""
    tree = ast.parse(code)
    methods: dict[tuple[str, str], ast.AST] = {}
    owners: dict[tuple[str, str], str] = {}
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for item in cls.body:
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                methods[(cls.name, item.name)] = item
                if item.name == "__init__":
                    for node in ast.walk(item):
                        if (
                            isinstance(node, ast.Assign)
                            and isinstance(node.value, ast.Call)
                            and isinstance(node.value.func, ast.Name)
                        ):
                            for t in node.targets:
                                if (
                                    isinstance(t, ast.Attribute)
                                    and isinstance(t.value, ast.Name)
                                    and t.value.id == "self"
                                ):
                                    owners[(cls.name, t.attr)] = node.value.func.id
    return methods, owners


def test_CONTROL_a_same_named_method_on_another_class_does_not_satisfy_reachability():
    """Control: two classes defining `run` must not be conflated.

    Keyed by bare method name, the last class parsed wins and the guard reads a
    method the entry point never calls.
    """
    code = (
        "class Pipeline:\n"
        "    def run(self, call):\n"
        "        return call()\n"
        "class Unrelated:\n"
        "    def run(self, call):\n"
        "        return call()\n"
        "class Entry:\n"
        "    def __init__(self):\n"
        "        self.other = Unrelated()\n"
        "    def handle(self, call):\n"
        "        return self.other.run(call)\n"
    )
    methods, owners = _parse_classes(code)
    assert _reaches(("Entry", "handle"), ("Unrelated", "run"), methods, owners), (
        "positive half: the call that is really made must resolve"
    )
    assert not _reaches(("Entry", "handle"), ("Pipeline", "run"), methods, owners), (
        "a same-named method on a class the entry point never calls satisfied reachability"
    )


def test_CONTROL_a_call_in_an_unreachable_branch_does_not_satisfy_reachability():
    """Control: a call under a constant false test never runs, so it proves nothing."""
    code = (
        "class Pipeline:\n"
        "    def run(self, call):\n"
        "        return call()\n"
        "class Entry:\n"
        "    def __init__(self):\n"
        "        self.pipeline = Pipeline()\n"
        "    def handle(self, call):\n"
        "        if False:\n"
        "            self.pipeline.run(call)\n"
        "        return call()\n"
    )
    methods, owners = _parse_classes(code)
    assert not _reaches(("Entry", "handle"), ("Pipeline", "run"), methods, owners), (
        "a call parked under `if False:` satisfied reachability"
    )


def test_CONTROL_a_call_on_an_untyped_receiver_does_not_satisfy_reachability():
    """Control: `self.junk.run()` names no known class, so it is not a path."""
    code = (
        "class Pipeline:\n"
        "    def run(self, call):\n"
        "        return call()\n"
        "class Entry:\n"
        "    def handle(self, call):\n"
        "        return self.junk.run(call)\n"
    )
    methods, owners = _parse_classes(code)
    assert not _reaches(("Entry", "handle"), ("Pipeline", "run"), methods, owners), (
        "a call on a receiver of unknown type satisfied reachability"
    )


def test_CONTROL_a_local_named_dispatcher_passes_only_for_what_it_is_bound_to():
    """A local called `dispatcher` is read through its assignment: bound to the container's
    dispatcher it passes, bound to anything else it is reported under what it really is."""
    bound = (
        "class C:\n"
        "    async def sub(self, nc):\n"
        "        dispatcher = self._c.dispatcher\n"
        "        await nc.subscribe('x', cb=dispatcher.on_event)\n"
    )
    impostor = (
        "class C:\n"
        "    async def sub(self, nc):\n"
        "        dispatcher = self.service\n"
        "        await nc.subscribe('x', cb=dispatcher.on_event)\n"
    )

    assert _nats_callback_bindings(bound) == [("sub", "cb", "self.dispatcher.on_event")]
    assert _nats_callback_bindings(impostor) == [("sub", "cb", "self.service.on_event")]

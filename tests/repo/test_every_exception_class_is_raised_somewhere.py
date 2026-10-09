"""Every exception class the library defines is raised, or has a subclass that is.

A class nothing raises is a promise nothing keeps: code that catches it waits for an error that
cannot come. The resilience circuit breaker once counted failures by catching a `ConnectionError`
of this library's that no code raised, so it never opened. `test_no_orphan_defs.py` cannot see
this, because an exported class is referenced by its export.

A class is live when some `raise` under `src/` or `packages/*/src` raises it, or raises a
subclass of it: a base that exists to be caught is live through the classes that carry it. A
`raise` counts for a class only when the name it uses RESOLVES to that class through the module's
own definitions and imports, followed through re-exports. Matching on the bare name would let
`raise TimeoutError` (the builtin) or a pydantic `ValidationError` stand in for this library's
classes of the same names, and the guard would pass with the real ones dead.

Tests do not count: a class only a test raises is dead to every caller.

What it cannot see: a class raised through a computed name (`raise cls_for(code)(...)`), which is
reported. The way to clear a report is to raise the class, or to list it in
`NOT_RAISED_BY_THE_LIBRARY` with the reason it exists for somebody else to raise.
"""

import ast
import builtins
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

#: Qualified class name -> why the library defines it and never raises it. Empty: every class the
#: library defines is raised, directly or through a subclass. An entry is for a class that exists
#: for user code to raise or to subclass, and the reason says so. An entry for a class that is
#: raised, or that does not exist, fails the guard, so the list cannot go stale.
NOT_RAISED_BY_THE_LIBRARY: dict[str, str] = {}


def module_name(path: str) -> str:
    """`src/cliffracer/core/x.py` -> `cliffracer.core.x`; `packages/p/src/q/__init__.py` -> `q`."""
    parts = Path(path).with_suffix("").parts
    if "src" in parts:
        parts = parts[parts.index("src") + 1 :]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class Index:
    """The classes, aliases and imports of a set of source files, and what a name resolves to."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.trees: dict[str, ast.Module] = {}
        self.is_package: dict[str, bool] = {}
        self.classes: dict[str, list[str]] = {}  # qualified name -> raw base expressions' names
        self.class_module: dict[str, str] = {}
        self.imports: dict[str, dict[str, tuple[str, str | None]]] = {}
        self.aliases: dict[str, dict[str, str]] = {}
        for path, text in sources.items():
            module = module_name(path)
            self.trees[module] = ast.parse(text, filename=path)
            self.is_package[module] = Path(path).name == "__init__.py"
        for module, tree in self.trees.items():
            self.imports[module] = {}
            self.aliases[module] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    base = self._from_module(module, node)
                    for item in node.names:
                        self.imports[module][item.asname or item.name] = (base, item.name)
                elif isinstance(node, ast.Import):
                    for item in node.names:
                        bound = item.asname or item.name.split(".")[0]
                        target = item.name if item.asname else item.name.split(".")[0]
                        self.imports[module][bound] = (target, None)
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    self.classes[f"{module}.{node.name}"] = []
                    self.class_module[f"{module}.{node.name}"] = module
                elif (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Name)
                    and all(isinstance(t, ast.Name) for t in node.targets)
                ):
                    for target in node.targets:
                        assert isinstance(target, ast.Name)
                        self.aliases[module][target.id] = node.value.id

    def _from_module(self, module: str, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        package = module if self.is_package[module] else module.rpartition(".")[0]
        parts = package.split(".") if package else []
        parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
        return ".".join([*parts, *([node.module] if node.module else [])])

    def resolve(self, module: str, name: str, _seen: frozenset[tuple[str, str]] = frozenset()):
        """The qualified class `name` means inside `module`, or None (a builtin, a third party's)."""
        if (module, name) in _seen or module not in self.trees:
            return None
        seen = _seen | {(module, name)}
        if f"{module}.{name}" in self.classes:
            return f"{module}.{name}"
        if name in self.aliases[module]:
            return self.resolve(module, self.aliases[module][name], seen)
        if name in self.imports[module]:
            source, original = self.imports[module][name]
            if original is not None:
                return self.resolve(source, original, seen)
        return None

    def resolve_expression(self, module: str, node: ast.expr):
        """The class a raised or inherited expression names, or None."""
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, ast.Name):
            return self.resolve(module, node.id)
        if isinstance(node, ast.Attribute):
            owner = self._module_of(module, node.value)
            return self.resolve(owner, node.attr) if owner else None
        return None

    def _module_of(self, module: str, node: ast.expr):
        if isinstance(node, ast.Name):
            if node.id in self.imports[module]:
                source, original = self.imports[module][node.id]
                target = source if original is None else f"{source}.{original}"
                return target if target in self.trees else None
            return None
        if isinstance(node, ast.Attribute):
            parent = self._module_of(module, node.value)
            if parent is None:
                return None
            target = f"{parent}.{node.attr}"
            return target if target in self.trees else None
        return None

    def bases(self, qualified: str) -> list[str]:
        """Resolved library bases of a class; a builtin or third-party base is left out."""
        module = self.class_module[qualified]
        tree = self.trees[module]
        node = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and f"{module}.{n.name}" == qualified
        )
        out = []
        for base in node.bases:
            resolved = self.resolve_expression(module, base)
            if resolved:
                out.append(resolved)
        return out

    def raw_base_names(self, qualified: str) -> list[str]:
        module = self.class_module[qualified]
        node = next(
            n
            for n in self.trees[module].body
            if isinstance(n, ast.ClassDef) and f"{module}.{n.name}" == qualified
        )
        names = []
        for base in node.bases:
            target = base.func if isinstance(base, ast.Call) else base
            names.append(
                target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            )
        return names

    def exception_classes(self) -> set[str]:
        """Classes that are exceptions: through a builtin exception, or through another of these."""
        found: set[str] = set()
        changed = True
        while changed:
            changed = False
            for qualified in self.classes:
                if qualified in found:
                    continue
                library_bases = self.bases(qualified)
                builtin = any(
                    isinstance(getattr(builtins, raw, None), type)
                    and issubclass(getattr(builtins, raw), BaseException)
                    for raw in self.raw_base_names(qualified)
                )
                if builtin or any(base in found for base in library_bases):
                    found.add(qualified)
                    changed = True
        return found

    def raised(self) -> set[str]:
        """Every library class some `raise` names."""
        out: set[str] = set()
        for module, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and node.exc is not None:
                    resolved = self.resolve_expression(module, node.exc)
                    if resolved:
                        out.add(resolved)
        return out


def unraised(sources: dict[str, str], exempt: dict[str, str] | None = None) -> dict[str, list[str]]:
    """The exception classes nothing raises, each with what is wrong; empty when all are live.

    Also reports an exemption that is stale: a class that does not exist, or one that is raised.
    """
    exempt = NOT_RAISED_BY_THE_LIBRARY if exempt is None else exempt
    index = Index(sources)
    exceptions = index.exception_classes()
    raised = index.raised()
    children: dict[str, set[str]] = {}
    for qualified in exceptions:
        for base in index.bases(qualified):
            children.setdefault(base, set()).add(qualified)

    def live(qualified: str, seen: frozenset[str] = frozenset()) -> bool:
        if qualified in raised:
            return True
        return any(
            live(child, seen | {qualified})
            for child in children.get(qualified, ())
            if child not in seen
        )

    problems: dict[str, list[str]] = {}
    for qualified in sorted(exceptions):
        if live(qualified):
            continue
        if qualified in exempt:
            continue
        problems[qualified] = ["no `raise` names this class or any subclass of it"]
    for qualified, reason in exempt.items():
        if not reason.strip():
            problems.setdefault(qualified, []).append("an exemption must say why")
        if qualified not in exceptions:
            problems.setdefault(qualified, []).append(
                "exempt, but it is not an exception class here"
            )
        elif live(qualified):
            problems.setdefault(qualified, []).append("exempt, but it is raised: drop the entry")
    return problems


def _repo_sources() -> dict[str, str]:
    paths = [*REPO.joinpath("src").rglob("*.py"), *REPO.glob("packages/*/src/**/*.py")]
    return {str(p.relative_to(REPO)): p.read_text() for p in sorted(paths)}


def test_every_exception_class_the_library_defines_is_raised_or_has_a_subclass_that_is():
    sources = _repo_sources()
    found = Index(sources).exception_classes()
    assert len(found) >= 40, (
        f"the scan found {len(found)} exception classes; it is reading too little"
    )
    assert "cliffracer.core.exceptions.RpcTimeoutError" in found

    problems = unraised(sources)

    assert not problems, "exception classes nothing raises:\n" + "\n".join(
        f"  {name}: {'; '.join(why)}" for name, why in problems.items()
    )


# ---- controls: the same function over synthetic sources, each a way the guard could pass wrongly ----

CORE = "src/cliffracer/core/exceptions.py"
LEAF = "class Base(Exception):\n    pass\n\n\nclass Leaf(Base):\n    pass\n"


def _flagged(files: dict[str, str], exempt: dict[str, str] | None = None) -> set[str]:
    return set(unraised(files, exempt or {}))


def test_CONTROL_a_class_nothing_raises_is_reported():
    assert _flagged({CORE: LEAF}) == {
        "cliffracer.core.exceptions.Base",
        "cliffracer.core.exceptions.Leaf",
    }


def test_CONTROL_a_raised_leaf_makes_its_base_live_too():
    files = {
        CORE: LEAF,
        "src/cliffracer/use.py": "from .core.exceptions import Leaf\n\ndef f():\n    raise Leaf('x')\n",
    }

    assert _flagged(files) == set()


def test_CONTROL_a_raised_base_does_not_make_its_unraised_subclass_live():
    files = {
        CORE: LEAF,
        "src/cliffracer/use.py": "from .core.exceptions import Base\n\ndef f():\n    raise Base\n",
    }

    assert _flagged(files) == {"cliffracer.core.exceptions.Leaf"}


def test_CONTROL_a_bare_name_that_is_a_builtin_does_not_stand_in_for_the_librarys_class():
    files = {
        CORE: "class TimeoutError(Exception):\n    pass\n",
        "src/cliffracer/use.py": "def f():\n    raise TimeoutError('builtin')\n",
    }

    assert _flagged(files) == {"cliffracer.core.exceptions.TimeoutError"}


def test_CONTROL_a_third_partys_class_of_the_same_name_does_not_stand_in_either():
    files = {
        CORE: "class ValidationError(Exception):\n    pass\n",
        "src/cliffracer/use.py": "from pydantic import ValidationError\n\ndef f():\n    raise ValidationError('x')\n",
    }

    assert _flagged(files) == {"cliffracer.core.exceptions.ValidationError"}


def test_CONTROL_a_raise_through_an_alias_a_re_export_or_a_module_attribute_counts():
    exceptions = "class Real(Exception):\n    pass\n\n\nAlias = Real\n"
    reexport = "from .core.exceptions import Alias as Exported\n"
    via_alias = {
        CORE: exceptions,
        "src/cliffracer/use.py": "from .core.exceptions import Alias\n\ndef f():\n    raise Alias()\n",
    }
    via_export = {
        CORE: exceptions,
        "src/cliffracer/__init__.py": reexport,
        "src/other/use.py": "from cliffracer import Exported\n\ndef f():\n    raise Exported()\n",
    }
    via_attr = {
        CORE: exceptions,
        "src/cliffracer/use.py": "from .core import exceptions\n\ndef f():\n    raise exceptions.Real('x')\n",
        "src/cliffracer/core/__init__.py": "",
    }

    assert _flagged(via_alias) == set()
    assert _flagged(via_export) == set()
    assert _flagged(via_attr) == set()


def test_CONTROL_the_sources_read_are_the_librarys_own_and_no_test():
    paths = list(_repo_sources())

    assert paths, "no sources read"
    assert all(path.startswith(("src/", "packages/")) and "/src/" in f"/{path}" for path in paths)
    assert not [path for path in paths if "/tests/" in path or path.startswith("tests/")]


def test_CONTROL_an_exemption_clears_a_report_and_a_stale_one_fails():
    files = {
        CORE: "class UserRaises(Exception):\n    pass\n\n\nclass Raised(Exception):\n    pass\n",
        "src/cliffracer/use.py": "from .core.exceptions import Raised\n\ndef f():\n    raise Raised()\n",
    }
    name = "cliffracer.core.exceptions.UserRaises"

    assert _flagged(files, {name: "for user code to raise"}) == set()
    assert _flagged(files, {name: " "}) == {name}
    assert _flagged(files, {"cliffracer.core.exceptions.Raised": "x", name: "y"}) == {
        "cliffracer.core.exceptions.Raised"
    }
    assert _flagged(files, {"cliffracer.core.exceptions.Gone": "x", name: "y"}) == {
        "cliffracer.core.exceptions.Gone"
    }

"""Module-level functions and classes that nothing references.

A definition counts as referenced when another file imports it, when another
file reads it off a module it imported, when its own file names it as a bare
identifier, or when its own file lists it in __all__. Anything else -- a
same-named local, a parameter, an annotation target, a `self.x` attribute, a
string equal to the name -- is a coincidence and does not exempt it.

Two shapes are exempt, each because pytest reaches them without naming them:
`test_*` collected by convention, and a fixture SOMETHING CAN REACH -- autouse,
named as a parameter by a test or another fixture, named in `usefixtures`, or a
conftest fixture overriding one of the same name in a wider scope. A fixture
nothing can ask for is reported: "pytest could reach it" is not "anything does".

Because those fixtures live in the test tree, an unreachable fixture is reported
wherever it is defined, while every other shape is reported only under src/,
packages/ and tools/. A leading underscore exempts nothing.

Reachability that never spells the name in source, such as `getattr` on a
computed string or a registry keyed by text, is invisible to this sweep and
will be reported.
"""

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "packages", "tests", "examples", "tools", "scripts")
# tools/ is included because generators and one-shot utility scripts reside there.
# scripts/ holds what CI runs; a definition only a script calls is not dead, and
# a script's own dead definition is as dead as any other.


def _py_files(root: Path):
    # THE REPOSITORY ROOT'S OWN SCRIPTS TOO, not recursively: everything below
    # the root that is scanned is named in SCAN_DIRS. A root script is a caller
    # like any other, and leaving it out made its callees read as dead -- one
    # was deleted as dead, and the script that called it failed from then on
    # with nothing running it to notice.
    yield from sorted(root.glob("*.py"))
    for d in SCAN_DIRS:
        base = root / d
        if base.exists():
            yield from base.rglob("*.py")


# THE ONE EXEMPTION, AS A RULE RATHER THAN A LIST OF NAMES. A name list needs
# editing whenever a class is added and fails open when nobody remembers; a rule
# states the property that makes the class reachable, so a new class either has
# that property or is swept.
#
# pytest collects `Test*` classes by convention, so nothing references them by
# name. Restricted to files under a tests/ directory: a `TestHarness` in src/ is
# a real orphan, and a bare prefix rule would exempt it.
#
# The report filter below keeps only files under src/, packages/ and tools/, so
# test classes in the root tests/ tree are out of scope and never reach this
# exemption. The rule applies to classes under packages/*/tests/.
#
# No exemption is needed for exported exceptions: package __init__ files that
# export exception types import them, and imports count as references.
# test_an_exported_exception_is_reachable_because_the_export_is_a_reference
# verifies this behavior.


class SweepCannotAnswer(Exception):
    """A file under the sweep could not be read, so no verdict is available.

    NOT an orphan report, and deliberately not a silent skip. This sweep decides
    reachability: a definition is dead when NOTHING references it. A file that
    is skipped contributes no references, so every definition it referenced
    becomes an orphan -- a transient read failure would be reported as a list of
    dead code, which is worse than no answer at all.

    `unreachable_fixtures` had exactly that shape: `except SyntaxError: continue`
    dropped that file's fixture requests, so a fixture it requested read as
    unrequested. The other three parse sites had no guard and raised, which
    produced a traceback with no orphan list and no statement of what went
    wrong.

    So a read failure is one named condition, raised once, naming the file.
    """


def _read_tree(path: Path) -> ast.Module:
    """Parse one file, or say which file the sweep could not read.

    Every parse in this module goes through here. `SyntaxError` covers a file
    that is invalid or half-written; `OSError` covers one that vanished or
    became unreadable between the walk and the read; `UnicodeDecodeError`
    covers one being rewritten as bytes.
    """
    try:
        return ast.parse(path.read_text(), filename=str(path))
    except (SyntaxError, OSError, UnicodeDecodeError) as exc:
        raise SweepCannotAnswer(f"could not read {path}: {type(exc).__name__}: {exc}") from exc


def _in_tests_dir(path: Path) -> bool:
    return "tests" in path.parts


def _fixture_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, bool]:
    """(pytest binds this by decorator, it binds it into every test).

    Covers `@pytest.fixture` and a bare `@fixture`, called or not, and reads
    `autouse=` when it is spelled as a literal.
    """
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if name != "fixture":
            continue
        autouse = False
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if (
                    keyword.arg == "autouse"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value
                ):
                    autouse = True
        return True, autouse
    return False, False


# WHY THIS IS NOT "EVERY FIXTURE IS EXEMPT", WHICH IS WHAT IT USED TO SAY.
#
# The reasoning for the old exemption was sound for a fixture some test uses --
# pytest binds it through the decorator, so nothing spells its name -- and the
# exemption it produced was wider than the reasoning. A fixture nothing requests
# is exactly the dead definition this guard exists to report, and it was the one
# shape the guard could not see.
#
# The four ways something can reach a fixture are all statically checkable, so
# the rule states them rather than exempting the shape.
def unreachable_fixtures(paths: list[Path]) -> set[tuple[Path, str]]:
    """Every fixture nothing in the tree can ask pytest for.

    A requester is a test function or another fixture: those are the two things
    pytest resolves a fixture name for. A parameter of ordinary library code is
    not one -- `add_nats_sink(nats_connection=...)` under packages/ must not
    exempt a `nats_connection` fixture -- which is why this does not simply
    count every parameter in the tree.

    A conftest name defined more than once is treated as reachable. pytest
    resolves a conftest fixture that shadows one of the same name from a wider
    scope at collection time, and a syntactic sweep cannot see through that, so
    this exempts a shape it cannot decide rather than reporting a guess.

    COMPUTED TO A FIXED POINT, because a fixture requested only by a dead
    fixture is dead too. Without the loop the report peels one layer per run:
    `test_config`'s only requester is `test_service`, which nothing requests, so
    a single pass names `test_service` alone and a reader fixes it and finds the
    next one. A guard that reports eight of nine is a guard that has to be run
    again to be believed.
    """
    fixtures: dict[tuple[Path, str], bool] = {}  # (path, name) -> autouse
    requesters: list[tuple[Path, str, bool, frozenset[str]]] = []  # who asks for what
    usefixtures: set[str] = set()
    conftest_names: Counter[str] = Counter()

    for path in paths:
        # NOT `except SyntaxError: continue`. Dropping a file here loses its
        # fixture requests, and a fixture nothing is left requesting reads as
        # dead -- a read failure would be reported as a list of dead fixtures.
        tree = _read_tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                is_fixture, autouse = _fixture_decorator(node)
                if is_fixture:
                    fixtures[(path, node.name)] = autouse
                    if path.name == "conftest.py":
                        conftest_names[node.name] += 1
                if is_fixture or node.name.startswith("test_"):
                    args = node.args
                    wanted = frozenset(
                        arg.arg
                        for arg in args.posonlyargs + args.args + args.kwonlyargs
                        if arg.arg not in ("self", "cls")
                    )
                    requesters.append((path, node.name, is_fixture, wanted))
            elif isinstance(node, ast.Call):
                target = node.func
                name = (
                    target.attr
                    if isinstance(target, ast.Attribute)
                    else getattr(target, "id", None)
                )
                if name == "usefixtures":
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            usefixtures.add(arg.value)

    overridden = {name for name, count in conftest_names.items() if count > 1}

    dead: set[tuple[Path, str]] = set()
    while True:
        asked = {
            name
            for path, owner, is_fixture, wanted in requesters
            if not (is_fixture and (path, owner) in dead)
            for name in wanted
        }
        found = {
            (path, name)
            for (path, name), autouse in fixtures.items()
            if not autouse
            and name not in asked
            and name not in usefixtures
            and not (path.name == "conftest.py" and name in overridden)
        }
        if found == dead:
            return dead
        dead = found


def _defs(path: Path, dead_fixtures: set[tuple[Path, str]]):
    """Module-level defs AND classes, minus the exempt shapes.

    Yields `(lineno, name, is_unreachable_fixture)`. The third element decides
    WHERE the name can be reported: an unreachable fixture is reported wherever
    it lives, because fixtures live in the test tree, which is otherwise only a
    source of references.
    """
    tree = _read_tree(path)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            # pytest calls a conftest's `pytest_*` hooks by name, as it collects
            # `test_*`: nothing spells them, and they run on every session.
            if path.name == "conftest.py" and node.name.startswith("pytest_"):
                continue
            is_fixture, _ = _fixture_decorator(node)
            if is_fixture:
                if (path, node.name) not in dead_fixtures:
                    continue
                yield node.lineno, node.name, True
                continue
            yield node.lineno, node.name, False
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("Test") and _in_tests_dir(path):
                continue
            yield node.lineno, node.name, False


def _first_party_roots(root: Path) -> frozenset[str]:
    """Top-level import names that resolve to code in this repository.

    Derived from the tree rather than listed, so a new member package is
    first-party the moment it exists.
    """
    roots = set(SCAN_DIRS) | {"scripts"}
    # A module at the repository root is imported by its bare name --
    # `from conftest import console_script` -- so its stem is a root too. So is
    # a script's: tests put scripts/ on the path and import from it the same
    # way, `from check_benchmark_regression import ...`.
    roots.update(f.stem for f in root.glob("*.py"))
    roots.update(f.stem for f in (root / "scripts").glob("*.py"))
    for base in [root / "src", *(root / "packages").glob("*/src")]:
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir() and (child / "__init__.py").exists():
                roots.add(child.name)
            elif child.suffix == ".py":
                roots.add(child.stem)
    return frozenset(roots)


def _module_paths(root: Path) -> frozenset[str]:
    """Dotted import paths of every module in this repository.

    Used to tell `from pkg import submodule` (a module, whose attributes are
    references) from `from pkg import SomeClass` (an object, whose attributes
    are its own members).
    """
    paths: set[str] = set()

    def walk(base: Path, prefix: str) -> None:
        if not base.is_dir():
            return
        for f in base.rglob("*.py"):
            rel = f.relative_to(base).with_suffix("")
            parts = [p for p in rel.parts if p != "__init__"]
            if parts:
                paths.add(".".join(([prefix] if prefix else []) + parts))

    walk(root / "src", "")
    for pkg_src in (root / "packages").glob("*/src"):
        walk(pkg_src, "")
    for d in SCAN_DIRS:
        if d not in ("src", "packages"):
            walk(root / d, d)
    return frozenset(paths)


def _package_of(root: Path, path: Path) -> str | None:
    """The dotted package a relative import in `path` is resolved against, by the rules
    `_module_paths` names modules with, or None for a file outside them."""
    bases = [(root / "src", ""), *((p, "") for p in (root / "packages").glob("*/src"))]
    bases += [(root / d, d) for d in SCAN_DIRS if d not in ("src", "packages")]
    for base, prefix in bases:
        if path.is_relative_to(base):
            # The package is the file's directory: `pkg/mod.py` and `pkg/__init__.py` are in `pkg`.
            parts = list(path.relative_to(base).parts[:-1])
            return ".".join(([prefix] if prefix else []) + parts)
    return None


def _first_party_bindings(
    tree: ast.AST,
    first_party: frozenset[str],
    module_paths: frozenset[str],
    package: str | None = None,
) -> tuple[set[str], dict[str, str]]:
    """Return (names imported from this repo, each alias bound to one of this repo's modules, with
    the module's dotted path). A relative import is resolved against `package`, the importing
    file's own.

    An import of a third-party package binds a name too, but not one of ours:
    `nats.connect` says nothing about a `connect` defined in this repository.
    Nor does `Client.connect` -- that alias is a class, not a module.
    """
    imported: set[str] = set()
    module_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                head = a.name.split(".")[0]
                if head in first_party:
                    # A module import binds a module, which reaches no definition by name; what is
                    # read off it is counted through `module_aliases`. `import a.b` binds `a`.
                    if a.asname:
                        module_aliases[a.asname] = a.name
                    else:
                        module_aliases[head] = head
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            head = module.split(".")[0]
            if not (node.level or head in first_party):
                continue
            if node.level and package is not None:
                parents = package.split(".") if package else []
                kept = parents[: len(parents) - (node.level - 1)] if node.level > 1 else parents
                module = ".".join([*kept, *([module] if module else [])])
            for a in node.names:
                bound = a.asname or a.name
                # The definition is reached by its own name. An alias is only what this file binds,
                # so it reaches no other file's definition of that name; a module bound under one
                # is read through `module_aliases`.
                imported.add(a.name)
                candidate = f"{module}.{a.name}" if module else a.name
                if candidate in module_paths:
                    module_aliases[bound] = candidate
                elif a.name in module_paths:
                    module_aliases[bound] = a.name
    return imported, module_aliases


# A `module:Name` target string, the form this project's CLI resolves.
#
# THE SHAPE ALONE IS NOWHERE NEAR ENOUGH, and measuring said so: `word:word`
# matched 110 strings in this tree, of which about 104 are not targets. This is
# a NATS project, so `"orders:write"` and `"tenant:api"` are everywhere, and
# `"no:cacheprovider"` is a pytest option. Among them sat `":setup"`, which
# would have exempted a dead `def setup()` anywhere in the tree, for good.
#
# So three things are required together, and each one drops a different class of
# false match:
#
#   1. the whole string matches, not a substring  -- excludes prose
#   2. the module half is a REAL first-party module path (110 -> 6)
#   3. the string is an argument to a call         -- a target is passed to a
#      resolver; a subject string sitting in a dict is not
#
# The empty-module form is accepted only inside an f-string, because there the
# module is in the placeholder: `f"{MOD}:NotAService"` reaches the AST as the
# constant `":NotAService"` with `MOD` in a separate node, so rule 2 has nothing
# to check and rule 3 is what carries it.
TARGET_STRING = re.compile(r"^(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)?:(?P<name>[A-Za-z_]\w*)$")
FSTRING_TAIL = re.compile(r"^:(?P<name>[A-Za-z_]\w*)$")


# The functions that take a `module:Name` target. Named rather than guessed at,
# and `test_the_named_resolvers_exist` fails if one stops existing -- an
# exemption pointing at a function nobody has can never stop exempting.
RESOLVERS = ("resolve_targets",)


def _called_name(call: ast.Call) -> str | None:
    """`f` for `f(...)` and `m.f(...)`, or None for anything else."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _call_string_arguments(call: ast.Call):
    """Every string-ish node passed to *call*, through one level of list/tuple.

    `_generate(["--class", "pkg.mod:Service"])` and
    `resolve_targets([f"{MOD}:Service"])` are both this shape.
    """
    for arg in list(call.args) + [k.value for k in call.keywords]:
        if isinstance(arg, ast.List | ast.Tuple):
            yield from arg.elts
        else:
            yield arg


def _target_names(tree: ast.AST, module_paths: frozenset[str]) -> set[str]:
    """Names a `module:Name` target string hands to a resolver."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in _call_string_arguments(node):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found = TARGET_STRING.match(arg.value)
                if found and arg.value.rsplit(":", 1)[0] in module_paths:
                    out.add(found.group("name"))
            elif isinstance(arg, ast.JoinedStr) and _called_name(node) in RESOLVERS:
                # AN INTERPOLATED MODULE CARRIES NO EVIDENCE OF ITS OWN, so this
                # form needs positional evidence instead: it must be handed to
                # the resolver by name. Shape alone is not rare enough --
                # `events.append(f"{self.tag}:setup")` is the same shape, and
                # accepting it exempted a dead `def setup()` everywhere.
                seen_placeholder = False
                for part in arg.values:
                    if isinstance(part, ast.FormattedValue):
                        seen_placeholder = True
                    elif isinstance(part, ast.Constant) and isinstance(part.value, str):
                        tail = FSTRING_TAIL.match(part.value)
                        if tail and seen_placeholder:
                            out.add(tail.group("name"))
    return out


def _names(
    path: Path,
    first_party: frozenset[str],
    module_paths: frozenset[str],
    package: str | None = None,
) -> set[str]:
    """Collect the names by which this file can reach another file's definition.

    A module-level definition in another file is reachable three ways and no
    others: the name is imported from this repository, one of this repository's
    modules is imported and the name is read off it, or a string names it as a
    `module:Name` target. A bare identifier that was never imported is this
    file's own binding -- a parameter, a local, an annotation target -- and an
    attribute read off a third-party module belongs to that module. Naming the
    same word is a coincidence, not a reference.

    THE THIRD KIND IS NOT "ANY STRING CONTAINING THE WORD", which would exempt
    every definition whose name appears in prose. `TARGET_STRING` is anchored to
    the whole string and requires the colon, so it matches the shape this
    project's CLI actually resolves -- `resolve_targets("pkg.mod:Service")` --
    and not a sentence. The empty-module form is matched because an f-string
    splits at the placeholder: `f"{MOD}:NotAService"` reaches the AST as the
    constant `":NotAService"`, with the module in a separate node.
    """
    tree = _read_tree(path)
    imported, module_aliases = _first_party_bindings(tree, first_party, module_paths, package)
    out: set[str] = set(imported)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            # `alias.name`, or `alias.sub.name` when `alias.sub` is itself a module of ours. An
            # attribute read off anything else (a class held by the module) is not a module-level
            # definition reached by name.
            chain: list[str] = []
            root = node.value
            while isinstance(root, ast.Attribute):
                chain.insert(0, root.attr)
                root = root.value
            if isinstance(root, ast.Name) and root.id in module_aliases:
                if not chain or ".".join([module_aliases[root.id], *chain]) in module_paths:
                    out.add(node.attr)
    out |= _target_names(tree, module_paths)
    return out


def _all_exported_names(tree: ast.AST) -> set[str]:
    """Collect identifiers explicitly listed in module-level __all__ sequences."""
    exported: set[str] = set()
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, ast.List | ast.Tuple | ast.Set):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                exported.add(elt.value)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "__all__":
                if isinstance(node.value, ast.List | ast.Tuple | ast.Set):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            exported.add(elt.value)
    return exported


def _own_file_references(path: Path) -> set[str]:
    """The names a file's own code references or exports via __all__.

    Read once per file, not once per definition in it.
    """
    tree = _read_tree(path)

    # Identifiers in __all__ are part of the module public surface.
    out = _all_exported_names(tree)

    # A module-level definition is reached from inside its own file by a bare
    # identifier. `self.name` and `obj.name` are attribute lookups on some other
    # object and say nothing about the module-level name, so they do not count.
    # FunctionDef.name and ClassDef.name are strings, not ast.Name nodes, so any
    # ast.Name matching name is an actual reference.
    out.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
    return out


def orphan_defs(root: Path) -> list[tuple[Path, int, str]]:
    files = list(_py_files(root))
    first_party = _first_party_roots(root)
    module_paths = _module_paths(root)
    refs_by_file = {f: _names(f, first_party, module_paths, _package_of(root, f)) for f in files}
    # Which files reach each name, so a definition is looked up once, not against every file.
    reached_from: dict[str, set[Path]] = {}
    for other, refs in refs_by_file.items():
        for ref in refs:
            reached_from.setdefault(ref, set()).add(other)
    dead_fixtures = unreachable_fixtures(files)
    orphans = []
    for f in files:
        # WHERE ORPHANS ARE REPORTED, which is not the same as where names are
        # counted. Every SCAN_DIR contributes references; only these are
        # searched for orphans. Adding a directory to SCAN_DIRS alone makes it
        # a referencer and never a subject -- which is what "add tools/" looked
        # like it did, and did not.
        #
        # THE ONE EXCEPTION IS AN UNREACHABLE FIXTURE, because narrowing the
        # fixture rule without this reports nothing: every fixture in this
        # repository is defined in the test tree, which is not a subject. Two
        # separate reasons hid the shape, and fixing either alone leaves it
        # hidden.
        # EVERY SCAN_DIR IS A SUBJECT. It was src/packages/tools until the
        # dotted-path reference kind existed: widening without it reported two
        # definitions that are reached on every run, through
        # `resolve_targets("pkg.mod:Service")`, and a guard that reports live
        # code is a guard people learn to ignore. With `_names` reading those
        # strings, the test tree and examples can be held to the same rule as
        # the shipped source.
        in_report_scope = True
        own_references: set[str] | None = None
        for line, name, is_dead_fixture in _defs(f, dead_fixtures):
            if not in_report_scope and not is_dead_fixture:
                continue
            if is_dead_fixture:
                # NEITHER OF THE TWO CHECKS BELOW APPLIES TO A FIXTURE, and both
                # would wave this one through.
                #
                # The `test_*` convention does not: a fixture named
                # `test_config` is not collected as a test, because the fixture
                # decorator is what pytest sees -- and two of the fixtures this
                # first reported are named that way, so the convention skip
                # would have hidden exactly the cases the rule was narrowed for.
                #
                # Nor does spelling the name somewhere: a fixture is reached by
                # being requested, never by being named, so an identifier or a
                # string that matches is the coincidence this module refuses to
                # treat as a reference everywhere else. `test_service` occurs 33
                # times in the tree as a service name.
                orphans.append((f.relative_to(root), line, name))
                continue
            # pytest collects `test_*` by name convention, so nothing spells
            # them either. A leading underscore is not a reason: a private
            # module-level helper nothing calls is exactly as dead as a public
            # one.
            if name.startswith("test_"):
                continue
            if reached_from.get(name, set()) - {f}:
                continue
            if own_references is None:
                own_references = _own_file_references(f)
            if name not in own_references:
                orphans.append((f.relative_to(root), line, name))
    return orphans


def test_the_sweep_finds_a_planted_orphan(tmp_path: Path):
    """Positive control: the instrument must be able to fail."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "def used():\n    pass\n\ndef orphan():\n    pass\n\nused()\n"
    )
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/mod.py", 4, "orphan")]


def test_a_definition_imported_under_an_alias_is_referenced(tmp_path: Path):
    """`from mod import used_elsewhere as _u` reaches `used_elsewhere`, by its own name.

    The alias is what the importing file binds, and the definition is what it imports. Only the
    orphan beside it is reported.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "def used_elsewhere():\n    pass\n\ndef orphan():\n    pass\n"
    )
    (tmp_path / "src" / "user.py").write_text("from mod import used_elsewhere as _u\n\n_u()\n")
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/mod.py", 4, "orphan")]


def test_an_alias_is_not_a_reference_to_a_definition_of_that_name(tmp_path: Path):
    """`... import used_elsewhere as _u` reaches no other file's `_u`.

    The alias is a name the importing file binds for itself. A dead `def _u()` elsewhere is
    still dead, and the definition the alias stands for is the one reached.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def used_elsewhere():\n    pass\n")
    (tmp_path / "src" / "other.py").write_text("def _u():\n    pass\n")
    (tmp_path / "src" / "user.py").write_text("from mod import used_elsewhere as _u\n\n_u()\n")
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/other.py", 1, "_u")]


def test_a_module_imported_under_an_alias_is_not_a_reference_to_a_definition_of_that_name(
    tmp_path: Path,
):
    """`import mod as _u` binds a module. It reaches no definition by the name `_u`, so a dead
    `def _u()` elsewhere is still dead."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def helper():\n    pass\n")
    (tmp_path / "src" / "other.py").write_text("def _u():\n    pass\n")
    (tmp_path / "src" / "user.py").write_text("import mod as _u\n\n_u.helper()\n")
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/other.py", 1, "_u")]


def test_CONTROL_a_definition_read_off_a_module_alias_is_referenced(tmp_path: Path):
    """The module's attributes, read through the alias, are what such an import reaches."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def helper():\n    pass\n\ndef orphan():\n    pass\n")
    (tmp_path / "src" / "user.py").write_text("import mod as m\n\nm.helper()\n")
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/mod.py", 4, "orphan")]


def _package_with_a_helper(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        "def helper():\n    pass\n\ndef orphan():\n    pass\n"
    )


def test_a_definition_read_off_a_dotted_module_path_is_referenced(tmp_path: Path):
    """`import pkg.mod` binds `pkg`; `pkg.mod.helper` reads `helper` off the module `pkg.mod`."""
    _package_with_a_helper(tmp_path)
    (tmp_path / "src" / "user.py").write_text("import pkg.mod\n\npkg.mod.helper()\n")
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/pkg/mod.py", 4, "orphan")]


def test_a_definition_read_off_a_relatively_imported_module_is_referenced(tmp_path: Path):
    """`from . import mod` inside `pkg` binds the module `pkg.mod`."""
    _package_with_a_helper(tmp_path)
    (tmp_path / "src" / "pkg" / "user.py").write_text("from . import mod\n\nmod.helper()\n")
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/pkg/mod.py", 4, "orphan")]


def test_CONTROL_an_attribute_of_a_class_read_off_a_module_reaches_no_definition(tmp_path: Path):
    """`m.Thing.method` reads `Thing` off the module and `method` off the class: a dead
    module-level `def method()` elsewhere is still dead."""
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        "class Thing:\n    def method(self):\n        pass\n"
    )
    (tmp_path / "src" / "other.py").write_text("def method():\n    pass\n")
    (tmp_path / "src" / "user.py").write_text("import pkg.mod as m\n\nm.Thing.method()\n")
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/other.py", 1, "method")]


def test_a_definition_used_only_by_a_repository_root_script_is_referenced(tmp_path: Path):
    """A script at the repository root is a caller like any other.

    The root was not scanned, so a definition whose only caller was a root
    script read as dead, was deleted as dead, and the script broke without
    anything noticing.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def helper():\n    pass\n")
    (tmp_path / "check_package.py").write_text("from mod import helper\n\nhelper()\n")

    found = {(str(f), name) for f, _, name in orphan_defs(tmp_path)}

    assert ("src/mod.py", "helper") not in found, found


def test_the_sweep_finds_a_planted_orphan_in_a_repository_root_script(tmp_path: Path):
    """The root is a subject as well as a referrer."""
    (tmp_path / "check_package.py").write_text("def orphan():\n    pass\n")

    found = [(str(f), line, name) for f, line, name in orphan_defs(tmp_path)]

    assert found == [("check_package.py", 1, "orphan")], found


def test_a_helper_imported_from_a_root_conftest_is_referenced(tmp_path: Path):
    """A root module is imported by its bare name, so that name is first-party."""
    (tmp_path / "conftest.py").write_text("def helper():\n    pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "from conftest import helper\n\ndef test_it():\n    helper()\n"
    )

    found = {(str(f), name) for f, _, name in orphan_defs(tmp_path)}

    assert ("conftest.py", "helper") not in found, found


def test_a_conftest_hook_is_exempt_and_the_same_name_elsewhere_is_not(tmp_path: Path):
    """The exemption is the hook convention, which only a conftest.py has."""
    (tmp_path / "conftest.py").write_text("def pytest_configure(config):\n    pass\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def pytest_configure(config):\n    pass\n")

    found = [(str(f), name) for f, _, name in orphan_defs(tmp_path)]

    assert found == [("src/mod.py", "pytest_configure")], found


def test_a_definition_used_only_by_a_script_is_referenced(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def helper():\n    pass\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text("from mod import helper\n\nhelper()\n")

    found = {(str(f), name) for f, _, name in orphan_defs(tmp_path)}

    assert ("src/mod.py", "helper") not in found, found


def test_the_sweep_finds_a_planted_orphan_in_a_script(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text("def orphan():\n    pass\n")

    found = [(str(f), line, name) for f, line, name in orphan_defs(tmp_path)]

    assert found == [("scripts/tool.py", 1, "orphan")], found


def test_a_script_function_used_only_by_a_test_is_referenced(tmp_path: Path):
    """Tests import a script by its bare name, so that name is first-party."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text("def compare():\n    pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_tool.py").write_text(
        "from tool import compare\n\ndef test_it():\n    compare()\n"
    )

    found = {(str(f), name) for f, _, name in orphan_defs(tmp_path)}

    assert ("scripts/tool.py", "compare") not in found, found


def test_the_sweep_finds_a_planted_orphan_class(tmp_path: Path):
    """Positive control for the widening: a class nothing names is an orphan.

    The def half has its own control above. Widening the walk without one would
    leave the class half asserted by a check nothing had shown could fail for
    classes -- which is the same gap as a licence check whose control removes
    one member's file.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "class Used:\n    pass\n\n\nclass Orphan:\n    pass\n\n\nUsed()\n"
    )
    found = orphan_defs(tmp_path)
    assert [(str(f), line, name) for f, line, name in found] == [("src/mod.py", 5, "Orphan")]


def test_the_Test_exemption_does_not_reach_outside_a_tests_directory(tmp_path: Path):
    """The exemption's own negative: a bare `Test*` rule would exempt src/ too.

    An exemption that exempts more than its reason covers is worse than none,
    because it is invisible: `TestHarness` in src/ is a real orphan and pytest
    is not collecting it.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("class TestHarness:\n    pass\n")
    (tmp_path / "packages" / "p" / "tests").mkdir(parents=True)
    (tmp_path / "packages" / "p" / "tests" / "test_x.py").write_text("class TestThing:\n    pass\n")

    found = {name for _, _, name in orphan_defs(tmp_path)}
    assert "TestHarness" in found, (
        "a Test* class in src/ is not collected by pytest and is an orphan"
    )
    assert "TestThing" not in found, "a Test* class under tests/ is collected by convention"


def test_an_exported_exception_is_reachable_because_the_export_is_a_reference(tmp_path: Path):
    """Why there is no exemption for exported exceptions -- asserted, not assumed.

    "An exception exists to be caught, so exported ones are exempt" reads like a
    rule this sweep needs, and it is inert: the __init__ that exports a name
    IMPORTS it, and an import is a reference, so an exported exception never
    reaches the orphan list to be exempted from it. This pins the property the
    absent exemption relies on, so a change to _names() that stopped counting
    aliases would fail here rather than silently start flagging every exported
    exception in the tree.
    """
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "exceptions.py").write_text(
        "class Exported(Exception):\n    pass\n\n\nclass Unexported(Exception):\n    pass\n"
    )
    (tmp_path / "src" / "pkg" / "__init__.py").write_text(
        'from pkg.exceptions import Exported\n\n__all__ = ["Exported"]\n'
    )

    found = {name for _, _, name in orphan_defs(tmp_path)}
    assert "Exported" not in found, "the export is the reference; no exemption is needed"
    assert "Unexported" in found, (
        "negative half: an exception nothing raises and nothing exports cannot "
        "be caught either, which is what makes unexported sufficient"
    )


def sweep_reading(root: Path) -> str:
    """What the sweep actually read, for the failure text.

    This test failed once in a full-tier run and passed in every run since,
    including in isolation immediately afterwards. The message named the
    orphans and nothing else, so the one occurrence could not be told apart
    from a real finding -- and a repository guard that reds intermittently
    lands on whoever's pull request happens to be running, where the natural
    reading is "my diff did this".

    So the failure says what tree it judged. A count that differs from the
    usual one means the sweep saw something other than the checked-out tree,
    which is a different problem from a dead definition.
    """
    files = list(_py_files(root))
    dead = unreachable_fixtures(files)
    judged = sum(1 for f in _py_files(root) for _ in _defs(f, dead))
    return (
        f"swept {len(files)} files under {root}, judged {judged} definitions, "
        f"{len(dead)} unreachable fixtures"
    )


GONE = "   <-- THIS FILE NO LONGER EXISTS"


def orphan_rows(root: Path, found: list[tuple[Path, int, str]]) -> list[str]:
    """One line per orphan, marking any whose file is already gone.

    RE-STAT EACH REPORTED FILE, because the cheapest explanation for a one-off
    red is a file that existed while the sweep walked the tree and was gone by
    the time anyone looked. If that is what happened, the next occurrence says
    so here instead of leaving the reader to guess.
    """
    rows = []
    for f, line, name in found:
        marker = "" if (root / f).is_file() else GONE
        rows.append(f"  {f}:{line} {name}{marker}")
    return rows


def test_no_module_level_definition_is_unreferenced():
    try:
        found = orphan_defs(REPO)
    except SweepCannotAnswer as exc:
        pytest.fail(
            f"the sweep has no verdict: {exc}\n\n"
            "This is NOT a dead definition. A file under one of the scanned "
            "directories could not be read while the sweep walked the tree, so "
            "the reachability answer would have been computed from an incomplete "
            "set of references. Re-run; if it repeats, the named file is the "
            "subject, not whatever this guard would otherwise have reported."
        )
    rows = orphan_rows(REPO, found)

    assert not found, (
        "unreferenced module-level defs:\n"
        + "\n".join(rows)
        + f"\n\n{sweep_reading(REPO)}\n\n"
        + "A fixture here is one nothing can ask pytest for: not autouse, not "
        "named as a parameter by any test or fixture, not in a usefixtures, and "
        "not overriding a wider-scope conftest fixture of the same name. Delete "
        "it, or request it from the test that should have been using it.\n"
        "If a file above is marked as gone, or the swept count is not the "
        "usual one, this is not a dead definition -- the sweep read a tree "
        "that no longer exists."
    )


def test_the_sweep_subjects_every_scanned_directory_and_says_how_many():
    """A positive reading, because "no orphans" and "read nothing" look alike.

    The report scope is every SCAN_DIR now, not only the shipped source, so this
    records how many definitions are actually being judged. A change to the
    number is a change worth reading: it moves when a directory stops being
    scanned, when an exemption widens, or when the reference kinds change.
    """
    files = _py_files(REPO)
    dead_fixtures = unreachable_fixtures(list(files))
    judged = sum(1 for f in _py_files(REPO) for _ in _defs(f, dead_fixtures))

    assert judged > 500, f"only {judged} definitions were judged; the sweep is not reading the tree"
    roots = {str(f.relative_to(REPO)).split("/")[0] for f in _py_files(REPO)}
    for d in SCAN_DIRS:
        if (REPO / d).exists():
            assert d in roots, f"{d} is a SCAN_DIR but contributed no file to judge"


def test_CONTROL_a_dotted_path_target_string_exempts_its_class(tmp_path: Path):
    """The reference kind the CLI actually uses, in both spellings.

    `resolve_targets("pkg.mod:Service")` reaches a class without ever naming it
    as an identifier. Before this was a reference kind, widening the report
    scope reported two live classes -- and a guard that reports live code is one
    people learn to ignore.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "services.py").write_text(
        "class Alpha:\n    pass\n\n\nclass Beta:\n    pass\n"
    )
    (tmp_path / "src" / "caller.py").write_text(
        # `src/` is the package root, so the module path is `services`, and the
        # interpolated form counts only when the resolver itself is called.
        'MOD = "services"\nrun(["services:Alpha"])\nresolve_targets([f"{MOD}:Beta"])\n'
    )

    assert orphan_defs(tmp_path) == [], (
        "a class named only by a module:Name target string was reported dead"
    )


def test_the_named_resolvers_exist():
    """An exemption pointing at a function nobody has can never stop exempting."""
    sources = "\n".join(f.read_text() for f in _py_files(REPO) if f.is_relative_to(REPO / "src"))
    for name in RESOLVERS:
        assert f"def {name}(" in sources, (
            f"RESOLVERS names {name!r}, which no longer exists under src/. Either "
            f"it was renamed -- update this list -- or the f-string target form is "
            f"exempting nothing and should go."
        )


def test_CONTROL_a_subject_like_string_does_not_exempt_a_dead_definition(tmp_path: Path):
    """The measured failure this rule was narrowed to prevent.

    `word:word` matched 110 strings in the real tree and only six were targets.
    This is a NATS project: `"orders:write"` is a subject, `"tenant:api"` is a
    subject, `"no:cacheprovider"` is a pytest option. Worst of them was a
    literal `":setup"`, which under the first version of this rule exempted a
    dead `def setup()` ANYWHERE in the tree, permanently and silently.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def setup():\n    pass\n")
    (tmp_path / "src" / "user.py").write_text(
        "events = []\n"
        'events.append("orders:write")\n'
        'events.append("ext1:setup")\n'
        "tag = 'ext2'\n"
        'events.append(f"{tag}:setup")\n'
        'run(["localhost:notaport", "tenant:api"])\n'
    )

    found = [name for _, _, name in orphan_defs(tmp_path)]
    assert found == ["setup"], (
        f"a subject-shaped string exempted a dead definition: reported {found}"
    )


def test_CONTROL_a_target_naming_a_module_that_does_not_exist_does_not_exempt(
    tmp_path: Path,
):
    """The module half must be a real first-party module, not merely dotted.

    This is what takes the rule from 110 matches to six on the real tree.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("class Widget:\n    pass\n")
    (tmp_path / "src" / "user.py").write_text('run(["not.a.real.module:Widget"])\n')

    found = [name for _, _, name in orphan_defs(tmp_path)]
    assert found == ["Widget"], f"a dotted string that names no module exempted the class: {found}"


def test_CONTROL_a_target_string_outside_a_call_does_not_exempt(tmp_path: Path):
    """A target is handed to a resolver; one sitting in a dict is data."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("class Widget:\n    pass\n")
    (tmp_path / "src" / "user.py").write_text('ROUTES = {"a": "src.mod:Widget"}\n')

    found = [name for _, _, name in orphan_defs(tmp_path)]
    assert found == ["Widget"], f"a target string in a dict exempted the class: {found}"


def test_CONTROL_a_string_that_merely_contains_a_name_does_not_exempt_it(tmp_path: Path):
    """And the other side, so the reference kind is a shape rather than a word.

    Without the anchor, "see: Widget" in any docstring would exempt Widget for
    good. This is the row that fails if the pattern is ever loosened to a search.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "services.py").write_text("class Widget:\n    pass\n")
    (tmp_path / "src" / "caller.py").write_text(
        # The middle one is the row that matters: an unanchored search finds
        # `cache:Widget` inside it, because the colon is followed directly by an
        # identifier. A space after the colon would not match either way, so a
        # "see: Widget" example proves nothing about the anchor.
        '"""Prose that mentions Widget in passing."""\n'
        'LOG = "evicted cache:Widget from the pool"\n'
        'other = "Widget"\n'
    )

    found = [name for _, _, name in orphan_defs(tmp_path)]
    assert found == ["Widget"], f"a prose mention exempted the class: {found}"


def test_CONTROL_attribute_access_does_not_exempt_module_level_orphan(tmp_path: Path):
    """Control: Calling self.connect() in another file does not exempt dead module-level def connect()."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text(
        "class Service:\n    def run(self):\n        self.connect()\n"
    )
    (tmp_path / "src" / "messages.py").write_text("def connect():\n    return 1\n")

    found = orphan_defs(tmp_path)
    orphan_names = {name for _, _, name in found}
    assert "connect" in orphan_names, (
        "Attribute access self.connect() erroneously exempted module def connect"
    )


def test_CONTROL_string_constant_outside_dunder_all_does_not_exempt_orphan(tmp_path: Path):
    """Control: Mentioning function name in string constant does not exempt it unless in __all__."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        'LABEL = "orphan_func"\n\ndef orphan_func():\n    return 1\n'
    )

    found = orphan_defs(tmp_path)
    orphan_names = {name for _, _, name in found}
    assert "orphan_func" in orphan_names, (
        "String constant outside __all__ erroneously exempted orphan_func"
    )


def test_CONTROL_dunder_all_entry_properly_exempts_exported_definition(tmp_path: Path):
    """Control: __all__ entry properly counts as reference to module-level definition."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        '__all__ = ["exported_func"]\n\ndef exported_func():\n    return 1\n'
    )

    found = orphan_defs(tmp_path)
    orphan_names = {name for _, _, name in found}
    assert "exported_func" not in orphan_names, (
        "__all__ exported name should not be flagged as orphan"
    )


def test_CONTROL_same_file_attribute_access_does_not_exempt_module_level_orphan(tmp_path: Path):
    """Control: self.name() beside a dead def name() in the SAME file is not a reference.

    The cross-file version above cannot see this: the own-file check is a
    separate code path, and it exempted a def from any `self.x` in its module.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "def helper():\n    return 1\n\n\nclass Holder:\n    def go(self):\n        return self.helper()\n"
    )

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "helper" in orphan_names, "a same-file self.helper() exempted module def helper"


def test_CONTROL_an_annotation_target_does_not_exempt_a_like_named_orphan(tmp_path: Path):
    """Control: a dataclass field annotated with the same name is not a reference.

    An annotation target is an ast.Name, so a namespace flattened over every
    identifier counts it, and a dead def is exempted by a field it never meets.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lifecycle.py").write_text(
        "from collections.abc import Callable\n\n\nclass Hooks:\n    connect: Callable[[], None]\n"
    )
    (tmp_path / "src" / "messages.py").write_text("def connect():\n    return 1\n")

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "connect" in orphan_names, "a field annotation exempted module def connect"


def test_CONTROL_a_local_variable_does_not_exempt_a_like_named_orphan(tmp_path: Path):
    """Control: a local binding of the same word in another file is not a reference."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "uses.py").write_text(
        "def build():\n    publisher = object()\n    return publisher\n"
    )
    (tmp_path / "src" / "messages.py").write_text("def publisher():\n    return 1\n")

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "publisher" in orphan_names, "a local variable exempted module def publisher"


def test_CONTROL_a_third_party_module_attribute_does_not_exempt_an_orphan(tmp_path: Path):
    """Control: nats.connect() is that library's attribute, not this repository's def."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "client.py").write_text(
        "import nats\n\n\ndef dial():\n    return nats.connect()\n"
    )
    (tmp_path / "src" / "messages.py").write_text("def connect():\n    return 1\n")

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "connect" in orphan_names, "a third-party module attribute exempted module def connect"


def test_CONTROL_an_imported_class_attribute_does_not_exempt_an_orphan(tmp_path: Path):
    """Control: Client.connect is a member of that class, not this repository's def."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "client.py").write_text(
        "class Client:\n    def connect(self):\n        return 1\n"
    )
    (tmp_path / "src" / "cli.py").write_text(
        "from client import Client\n\n\ndef main():\n    return Client.connect\n"
    )
    (tmp_path / "src" / "messages.py").write_text("def connect():\n    return 1\n")

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "connect" in orphan_names, "an imported class attribute exempted module def connect"


def test_CONTROL_a_first_party_module_attribute_does_exempt_the_definition(tmp_path: Path):
    """Control: the tightening keeps the real reference path alive.

    Without this the rules above are satisfiable by a sweep that counts nothing
    at all, and every genuinely reachable definition would be reported.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "messages.py").write_text("def encode():\n    return 1\n")
    (tmp_path / "src" / "caller.py").write_text(
        "import messages\n\n\ndef go():\n    return messages.encode()\n"
    )

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "encode" not in orphan_names, "a genuine module-attribute reference was reported"
    assert "go" in orphan_names, "the control's own unreferenced def should still be reported"


def test_CONTROL_a_private_module_level_def_is_still_swept(tmp_path: Path):
    """A leading underscore is not a reason to be unreachable.

    A private helper left behind by a refactor is exactly as dead as a public
    one, and a blanket underscore exemption is what hid it.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "def _orphan():\n    return 1\n\n\ndef _used():\n    return 2\n\n\n_used()\n"
    )

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "_orphan" in orphan_names, "a private module-level def was exempted by its name"
    assert "_used" not in orphan_names, "a referenced private def was reported"


def test_CONTROL_a_reachable_fixture_is_not_swept(tmp_path: Path):
    """Every way something can ask pytest for a fixture, exempted.

    pytest binds a fixture through its decorator, so nothing in the tree spells
    its name. Without this the rule above would report every fixture in the
    suite; without the rule above, this one is satisfiable by exempting
    everything -- which is what the version of this control that asserted an
    UNREQUESTED fixture was exempt allowed, and what hid nine dead fixtures in
    `tests/conftest.py`.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "conftest.py").write_text(
        "import pytest\n\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _thing():\n"
        "    yield\n\n\n"
        "@pytest.fixture\n"
        "def asked_by_a_test():\n"
        "    return 1\n\n\n"
        "@pytest.fixture\n"
        "def asked_by_a_fixture():\n"
        "    return 2\n\n\n"
        "@pytest.fixture\n"
        "def asks_for_one(asked_by_a_fixture):\n"
        "    return asked_by_a_fixture\n\n\n"
        "@pytest.fixture\n"
        "def named_in_usefixtures():\n"
        "    return 3\n\n\n"
        "def _not_a_fixture():\n"
        "    return 4\n"
    )
    (tmp_path / "src" / "test_uses.py").write_text(
        "import pytest\n\n\n"
        "def test_one(asked_by_a_test, asks_for_one):\n"
        "    assert asked_by_a_test\n\n\n"
        '@pytest.mark.usefixtures("named_in_usefixtures")\n'
        "def test_two():\n"
        "    assert True\n"
    )

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    for exempt in (
        "_thing",
        "asked_by_a_test",
        "asked_by_a_fixture",
        "asks_for_one",
        "named_in_usefixtures",
    ):
        assert exempt not in orphan_names, f"the reachable fixture {exempt} was reported"
    assert "_not_a_fixture" in orphan_names, (
        "the undecorated helper beside them was not reported, so this control "
        "would pass with everything exempted"
    )


def test_CONTROL_a_fixture_nothing_requests_is_reported(tmp_path: Path):
    """The other half, and the shape the old exemption could not see.

    Not autouse, not requested, not in a usefixtures: nothing can ask pytest for
    it, so it is as dead as an uncalled helper.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef plain_thing():\n    return 1\n"
    )

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "plain_thing" in orphan_names, (
        "a fixture nothing requests was exempted, which is the whole blind spot"
    )


def test_CONTROL_a_dead_fixture_and_a_dead_helper_under_tests_are_both_reported(
    tmp_path: Path,
):
    """The test tree is a subject now, for fixtures and for everything else.

    This pinned the opposite until the dotted-path reference kind existed: the
    subject set was widened for fixtures ALONE, because widening it for every
    shape reported two classes that are reached through
    `resolve_targets("pkg.mod:Service")` and would have taught people to ignore
    the guard. With those strings counted as references, the same rule applies
    to the whole tree and both halves below are reported.

    The fixture half is kept because it is the one that could silently stop
    working: every fixture in this repository lives in the test tree, so if the
    test tree ever stops being a subject, the fixture rule finds nothing and
    says so by passing.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text(
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def dead_in_the_test_tree():\n"
        "    return 1\n\n\n"
        "def _dead_helper_in_the_test_tree():\n"
        "    return 2\n"
    )

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "dead_in_the_test_tree" in orphan_names, (
        "a dead fixture under tests/ was not reported, so narrowing the fixture "
        "rule alone would have found nothing"
    )
    assert "_dead_helper_in_the_test_tree" in orphan_names, (
        "an ordinary dead helper under tests/ was not reported, so the test tree "
        "is a source of references again and not a subject"
    )


def test_CONTROL_an_ordinary_parameter_of_the_same_name_does_not_exempt_a_fixture(
    tmp_path: Path,
):
    """A requester is a test or another fixture, not any function with the name.

    Live instance: `add_nats_sink(nats_connection=...)` under packages/ shares a
    name with a fixture in `tests/conftest.py`. Counting every parameter in the
    tree exempts the fixture on the strength of unrelated library code.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef nats_connection():\n    return 1\n"
    )
    (tmp_path / "src" / "lib.py").write_text(
        "def add_nats_sink(nats_connection):\n    return nats_connection\n"
    )

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "nats_connection" in orphan_names, (
        "a library function's parameter exempted a fixture nothing requests"
    )


def test_CONTROL_a_fixture_requested_only_by_a_dead_fixture_is_dead_too(tmp_path: Path):
    """The fixed point, without which the report peels one layer per run.

    Measured on this repository: `test_config`'s only requester was
    `test_service`, which nothing requested. A single pass names `test_service`
    alone, and the next run names `test_config`.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "conftest.py").write_text(
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def inner():\n"
        "    return 1\n\n\n"
        "@pytest.fixture\n"
        "def outer(inner):\n"
        "    return inner\n"
    )

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert orphan_names == {"inner", "outer"}, (
        f"both should be reported in one pass, got {sorted(orphan_names)}"
    )


def test_CONTROL_a_conftest_override_is_treated_as_reachable(tmp_path: Path):
    """The case a syntactic sweep cannot decide, exempted deliberately.

    pytest resolves a narrower conftest fixture that shadows a wider one at
    collection time, and the requesters are on the wider one. Treating a name
    defined in two conftests as reachable exempts a shape this sweep cannot see
    through, rather than reporting a guess.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef shared():\n    return 1\n"
    )
    (tmp_path / "src" / "inner").mkdir()
    (tmp_path / "src" / "inner" / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef shared():\n    return 2\n"
    )
    (tmp_path / "src" / "inner" / "test_it.py").write_text(
        "def test_uses(shared):\n    assert shared\n"
    )

    orphan_names = {name for _, _, name in orphan_defs(tmp_path)}
    assert "shared" not in orphan_names, "a conftest override was reported as an orphan"


def test_the_failure_text_says_what_tree_it_judged():
    """A one-off red has to be distinguishable from a real finding.

    This guard failed once in a full-tier run, passed in every run since, and
    its message named only the orphans -- so the occurrence could not be told
    apart from a dead definition. An intermittent repo guard reds on whoever's
    pull request is running, where the natural reading is "my diff did this".
    """
    reading = sweep_reading(REPO)

    assert "swept" in reading and "judged" in reading, reading
    numbers = [int(n) for n in re.findall(r"\d+", reading)]
    assert numbers and all(n > 0 for n in numbers[:2]), (
        f"the reading reports a zero, so it would say nothing on the run that matters: {reading}"
    )


def test_CONTROL_a_reported_file_that_is_gone_is_marked(tmp_path: Path):
    """The instrument, shown working on the case it exists for.

    Without this the marker could never appear and the next occurrence would be
    as mute as the first.
    """
    present = tmp_path / "here.py"
    present.write_text("def x():\n    pass\n")

    rows = orphan_rows(tmp_path, [(Path("here.py"), 1, "x"), (Path("vanished.py"), 1, "y")])

    assert rows[0].endswith("x"), f"a file that exists was marked as gone: {rows[0]}"
    assert rows[1].endswith(GONE), f"a file that is gone was not marked: {rows[1]}"


def test_CONTROL_the_marker_is_absent_when_every_file_is_there():
    """And it does not fire on the ordinary case, which is every real failure."""
    rows = orphan_rows(REPO, [(Path("tests/repo/test_no_orphan_defs.py"), 1, "orphan_rows")])

    assert GONE not in rows[0], rows[0]


# --- a file the sweep cannot read is a named condition, not a verdict ---------


def _tree_with_one_unreadable_file(tmp_path: Path, body: str) -> Path:
    """A tiny tree where `other.py` references `helper` and `broken.py` is bad.

    `helper` is reachable, so a sweep that skipped `broken.py` would still not
    report it -- the point of the reference is that the tree is otherwise sound,
    so any report at all is the failure.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "src" / "other.py").write_text("from src.lib import helper\n\nhelper()\n")
    broken = tmp_path / "src" / "broken.py"
    broken.write_text(body)
    return broken


def test_a_file_the_sweep_cannot_parse_yields_the_named_condition(tmp_path: Path):
    """Not a bare raise, and above all not a list of orphans.

    This is the shape the guard failed in: three parse sites had no guard, so an
    unparseable file raised `SyntaxError` out of `_names` and produced a
    traceback with no orphan list and no statement of what went wrong.
    """
    broken = _tree_with_one_unreadable_file(tmp_path, "def x(:\n")

    with pytest.raises(SweepCannotAnswer) as caught:
        orphan_defs(tmp_path)

    assert str(broken) in str(caught.value), caught.value
    assert "SyntaxError" in str(caught.value), caught.value


def test_a_file_the_sweep_cannot_read_yields_the_named_condition(tmp_path: Path):
    """The other way a file goes unreadable: it is gone by the time it is read.

    `SyntaxError` is only half of it -- a file that vanishes between the walk
    and the read raises `OSError`, which had no guard either.
    """
    broken = _tree_with_one_unreadable_file(tmp_path, "def y():\n    return 2\n")
    broken.unlink()
    broken.mkdir()  # a directory named *.py: present to the walk, unreadable

    with pytest.raises(SweepCannotAnswer) as caught:
        orphan_defs(tmp_path)

    assert str(broken) in str(caught.value), caught.value


def test_CONTROL_an_unreadable_file_does_not_produce_false_orphans(tmp_path: Path):
    """The soundness trap, pinned: skipping the file would report dead code.

    `unreachable_fixtures` used to `continue` past an unparseable file, which
    drops that file's fixture requests -- so a fixture it requested reads as
    unrequested. The same logic applies to every reference: skip a file and
    everything it referenced becomes an orphan. So the sweep must refuse to
    answer rather than answer from an incomplete tree.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.py").write_text("def only_referenced_by_broken():\n    return 1\n")
    (tmp_path / "src" / "broken.py").write_text(
        "from src.lib import only_referenced_by_broken\n\nonly_referenced_by_broken()\ndef z(:\n"
    )

    with pytest.raises(SweepCannotAnswer):
        orphan_defs(tmp_path)

    # And the shape that must never happen: a verdict naming the definition
    # whose only reference was in the file that could not be read.
    try:
        found = [name for _, _, name in orphan_defs(tmp_path)]
    except SweepCannotAnswer:
        found = None
    assert found is None, (
        f"the sweep answered from an incomplete tree and reported {found}; the "
        "only reference to that definition is in the file it could not read"
    )


def test_CONTROL_a_readable_tree_still_gets_a_verdict(tmp_path: Path):
    """The refusal is conditional, or every test above passes on a sweep that
    never answers anything."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.py").write_text(
        "def helper():\n    return 1\n\n\ndef dead():\n    return 2\n"
    )
    (tmp_path / "src" / "other.py").write_text("from src.lib import helper\n\nhelper()\n")

    found = [name for _, _, name in orphan_defs(tmp_path)]

    assert found == ["dead"], found

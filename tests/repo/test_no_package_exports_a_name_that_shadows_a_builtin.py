"""No distribution exports a name that is also a builtin's.

`from package import *` binds every name in `__all__`, so an exported
`ConnectionError` silently replaces the builtin in the importer's module, and an
`except ConnectionError` there stops catching real socket errors. The package
root did that for as long as it exported its own `ConnectionError`.

This reads each distribution's `__init__.py` rather than importing it, so a
package that cannot be imported on this host is still swept. Reading the source
means reading only the spellings of `__all__` the scanner understands: a literal
list or tuple of strings, assigned, annotated or added to with `+=`, at module
level. Any other mention of `__all__` (an `.append`, a concatenation, a call, an
assignment inside an `if`, a name that is not a string literal) is REFUSED with
the file and line, because a scanner that skipped it would report a clean tree
for exports it never saw. A second test imports each distribution that can be
imported and requires the scanner's names to equal its runtime `__all__`.
"""

import ast
import builtins
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


def _inits() -> list[Path]:
    return [
        REPO / "src" / "cliffracer" / "__init__.py",
        *sorted(REPO.glob("packages/*/src/*/__init__.py")),
    ]


class UnreadableExports(Exception):
    """A spelling of `__all__` this guard cannot read, so it cannot vouch for the names."""


def _is_all(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "__all__"


def exported_names(source: str, where: str = "<source>") -> list[str]:
    """The string names a module assigns to, or adds to, `__all__`; or raise UnreadableExports."""
    tree = ast.parse(source)
    names: list[str] = []
    read: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(_is_all(t) for t in node.targets):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AugAssign) and _is_all(node.target):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.AnnAssign) and _is_all(node.target) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        literal = isinstance(value, ast.List | ast.Tuple) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
        )
        if not literal:
            raise UnreadableExports(
                f"{where}:{node.lineno}: `__all__` is set from something other than a literal "
                "list or tuple of strings, so the names it exports are not known to this guard"
            )
        names += [e.value for e in value.elts]  # type: ignore[attr-defined]
        read.update(id(t) for t in targets if _is_all(t))
    for node in ast.walk(tree):
        if _is_all(node) and id(node) not in read:
            raise UnreadableExports(
                f"{where}:{node.lineno}: `__all__` is used in a way this guard does not read "
                "(a method call, a nested assignment, an annotation with no value); write it as "
                "a module-level literal list or tuple"
            )
    return names


def shadowing(source: str, where: str = "<source>") -> list[str]:
    return [name for name in exported_names(source, where) if hasattr(builtins, name)]


def test_no_exported_name_shadows_a_builtin():
    offenders = {
        str(path.relative_to(REPO)): found
        for path in _inits()
        if (found := shadowing(path.read_text(), str(path.relative_to(REPO))))
    }

    assert not offenders, (
        "these `__all__` lists export a name that is a builtin's, so a star-import "
        f"replaces the builtin in the importer's module: {offenders}"
    )


def test_CONTROL_the_sweep_reads_the_exports_that_exist():
    """An empty sweep passes for any reason: it must have seen the root and the
    extensions, and a substantial number of names."""
    inits = _inits()
    counts = [len(exported_names(p.read_text(), str(p))) for p in inits]

    assert len(inits) >= 9, inits
    assert all(count > 0 for count in counts), dict(zip(map(str, inits), counts, strict=True))
    assert sum(counts) >= 100, sum(counts)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('__all__ = ["ConnectionError"]', ["ConnectionError"]),
        ('__all__ = ["Fine", "TimeoutError"]', ["TimeoutError"]),
        ('__all__ = ("ValueError",)', ["ValueError"]),
        ('__all__ = ["Fine"]\n__all__ += ["open"]', ["open"]),
    ],
    ids=["list", "second-name", "tuple", "augmented"],
)
def test_CONTROL_a_shadowing_export_is_reported(source, expected):
    assert shadowing(source) == expected


def test_CONTROL_a_clean_export_is_not_reported():
    assert shadowing('__all__ = ["CliffracerService", "ServiceConfig"]') == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('__all__: list[str] = ["ConnectionError"]', ["ConnectionError"]),
        ('__all__: tuple[str, ...] = ("KeyError", "Fine")', ["KeyError", "Fine"]),
    ],
    ids=["annotated-list", "annotated-tuple"],
)
def test_an_annotated_assignment_is_read(source, expected):
    assert exported_names(source) == expected
    assert shadowing(source) == [name for name in expected if hasattr(builtins, name)]


@pytest.mark.parametrize(
    "source",
    [
        '__all__ = ["Fine"]\n__all__.append("KeyError")',
        '__all__ = ["Fine"]\n__all__.extend(["OSError"])',
        '__all__ = ["Fine"] + ["TimeoutError"]',
        'if True:\n    __all__ = ["ConnectionError"]',
        '__all__ = sorted(["ConnectionError"])',
        'from other import __all__ as base\n__all__ = [*base, "IOError"]',
        '__all__ = ["Fine"]\n__all__ += other.__all__',
        '__all__ = ["Fine", name]',
        '__all__: list[str]\n__all__ = ["Fine"]',
        'try:\n    __all__ = ["Fine"]\nexcept ImportError:\n    __all__ = []',
    ],
    ids=[
        "append",
        "extend",
        "concatenation",
        "nested-in-if",
        "call",
        "starred",
        "augmented-from-a-name",
        "a-name-element",
        "annotation-without-a-value",
        "nested-in-try",
    ],
)
def test_a_spelling_the_scanner_cannot_read_is_refused_by_file_and_line(source):
    with pytest.raises(UnreadableExports, match=r"pkg/__init__\.py:\d+: "):
        shadowing(source, "pkg/__init__.py")


def drift(source: str, where: str, runtime_all: list[str]) -> str | None:
    """How the names the scanner reads differ from the module's runtime `__all__`, or None."""
    scanned = set(exported_names(source, where))
    if scanned == set(runtime_all):
        return None
    return (
        f"read only in the source {sorted(scanned - set(runtime_all))}, "
        f"only at runtime {sorted(set(runtime_all) - scanned)}"
    )


def test_CONTROL_a_runtime_name_the_source_does_not_spell_out_is_reported():
    """The parity test can fail: a module whose runtime `__all__` carries a name its source never
    wrote (a spelling the scanner did not understand) differs, by name."""
    gap = drift('__all__ = ["A", "B"]', "pkg/__init__.py", ["A", "B", "KeyError"])

    assert gap is not None and "only at runtime ['KeyError']" in gap, gap


def test_CONTROL_a_source_name_that_is_not_exported_at_runtime_is_reported():
    gap = drift('__all__ = ["A", "B"]', "pkg/__init__.py", ["A"])

    assert gap is not None and "only in the source ['B']" in gap, gap


def test_CONTROL_identical_names_are_no_drift():
    assert drift('__all__ = ["A", "B"]', "pkg/__init__.py", ["B", "A"]) is None


def _module_name(init: Path) -> str:
    return init.parent.name


@pytest.mark.parametrize("init", _inits(), ids=lambda p: str(p.relative_to(REPO)))
def test_the_scanner_reads_exactly_the_runtime_all(init):
    """What the scanner reads is what an importer's `from package import *` binds."""
    name = _module_name(init)
    try:
        module = importlib.import_module(name)
    except ImportError as error:
        if init == _inits()[0]:
            raise  # the root is what the tests import: a skip here would skip every case
        pytest.skip(f"{name} cannot be imported on this host ({error}); only the scan covers it")

    gap = drift(init.read_text(), str(init.relative_to(REPO)), module.__all__)

    assert gap is None, f"{name}: {gap}"


def _unimportable(monkeypatch, *names: str) -> None:
    real = importlib.import_module

    def refuse(name, *args, **kwargs):
        if name in names:
            raise ImportError(f"{name} is not installed here")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", refuse)


def test_the_root_distribution_failing_to_import_fails_the_parity_test_rather_than_skipping(
    monkeypatch,
):
    """On a host where the root imports this branch is never taken, so it is driven here."""
    root = _inits()[0]
    _unimportable(monkeypatch, _module_name(root))

    with pytest.raises(BaseException) as outcome:  # a skip is a BaseException too
        test_the_scanner_reads_exactly_the_runtime_all(root)

    assert isinstance(outcome.value, ImportError), f"the root must raise, not {outcome.value!r}"
    assert "not installed here" in str(outcome.value)


def test_a_non_root_distribution_failing_to_import_is_skipped_by_name_with_the_reason(
    monkeypatch,
):
    other = _inits()[1]
    _unimportable(monkeypatch, _module_name(other))

    with pytest.raises(pytest.skip.Exception) as skipped:
        test_the_scanner_reads_exactly_the_runtime_all(other)

    assert str(skipped.value).startswith(f"{_module_name(other)} cannot be imported on this host (")
    assert "not installed here" in str(skipped.value)

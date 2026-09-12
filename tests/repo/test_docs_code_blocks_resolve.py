"""Documentation that points a reader at code which is not there.

WHAT THIS IS FOR. Before the 1.0 docs pass every guide in this repository
taught classes that had been deleted: 56 imports in fenced blocks did not
resolve, 15 blocks tagged ```python were not Python, and 14 relative links led
nowhere. Not one of them was caught by anything, because nothing read the
documentation. A reader following the README's HTTP example got an ImportError
on line 1.

WHAT IT DOES NOT DO, stated so a green run is not over-read: it PARSES blocks
and RESOLVES imports. It does not execute them, so a block that imports the
right names and then calls them wrongly passes here. Executable examples live
in examples/ and are run by tests/integration/test_examples_run.py; the
extensions guide's worked example is executed by
tests/integration/test_extensions_guide.py. This guard is the floor, not the
ceiling.

THE COUNT ASSERTIONS ARE NOT DECORATION. Every check here reports "0 problems"
when its extractor finds nothing at all -- a changed fence syntax, a moved
directory, a regex that stops matching. An all-zero result is what both success
and total failure look like, so each test also asserts it examined a plausible
number of things.
"""

import ast
import importlib
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# One `python` fence. Deliberately not `py`, `pycon`, `text` or `bash`: a
# console transcript is not source, and tagging one `python` is what made the
# original syntax-error count unreadable.
FENCE = re.compile(r"^```python\s*$(.*?)^```\s*$", re.S | re.M)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)")

# Documentation, not history. CHANGELOG.md is excluded on purpose: it describes
# what each release removed, so it MUST name things that no longer exist.
EXCLUDED = {"CHANGELOG.md"}

# Names deleted. None may appear in a python fence anywhere; prose may
# still name them to say they are gone, which is what most of the docs do.
REMOVED = [
    "HTTPNATSService",
    "WebSocketNATSService",
    "ValidatedNATSService",
    "BroadcastNATSService",
    "HighPerformanceService",
    "FullFeaturedService",
    "HTTPMixin",
    "WebSocketMixin",
    "PerformanceMixin",
    "ValidationMixin",
    "LoggingMixin",
    "DatabaseError",
    "cliffracer.core.mixins",
    "cliffracer.auth",
    "cliffracer.logging",
    "cliffracer.performance",
    "cliffracer.utils",
]
# NATSService is a SUFFIX of HTTPNATSService and WebSocketNATSService, so it
# needs a boundary on the left as well as the right. Getting this wrong reports
# every corrected line as still broken.
REMOVED_RE = {name: re.compile(rf"(?<![A-Za-z_.]){re.escape(name)}\b") for name in REMOVED}
REMOVED_RE["NATSService"] = re.compile(r"(?<![A-Za-z_])NATSService\b")


def docs() -> list[Path]:
    found = (
        set(REPO.glob("*.md"))
        | set(REPO.glob("docs/**/*.md"))
        | set(REPO.glob("packages/*/README.md"))
        | set(REPO.glob("examples/**/*.md"))
    )
    return sorted(d for d in found if d.name not in EXCLUDED)


def fences(doc: Path) -> list[tuple[int, str]]:
    text = doc.read_text()
    return [(text[: m.start()].count("\n") + 1, m.group(1)) for m in FENCE.finditer(text)]


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)  # a control's temporary document


def unparseable(paths=None) -> list[str]:
    out = []
    for doc in paths or docs():
        for line, body in fences(doc):
            try:
                ast.parse(body)
            except SyntaxError as exc:
                out.append(f"{_rel(doc)}:{line} {exc}")
    return out


def unresolvable_imports(paths=None) -> list[str]:
    out = []
    for doc in paths or docs():
        for line, body in fences(doc):
            try:
                tree = ast.parse(body)
            except SyntaxError:
                continue  # reported by the parse test; not double-counted here
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    pairs = [(a.name, None) for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level or not node.module:
                        continue  # relative import in a snippet: no package to resolve against
                    pairs = [(node.module, a.name) for a in node.names]
                else:
                    continue
                for module, attr in pairs:
                    try:
                        mod = importlib.import_module(module)
                    except Exception as exc:
                        out.append(f"{_rel(doc)}:{line} import {module} -> {exc!r}")
                        continue
                    if attr in (None, "*") or hasattr(mod, attr):
                        continue
                    try:
                        importlib.import_module(f"{module}.{attr}")
                    except Exception:
                        out.append(f"{_rel(doc)}:{line} from {module} import {attr} -> not there")
    return out


def broken_links(paths=None) -> list[str]:
    out = []
    for doc in paths or docs():
        for m in LINK.finditer(doc.read_text()):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if not (doc.parent / target.split("#", 1)[0]).exists():
                out.append(f"{_rel(doc)}: {target}")
    return out


def removed_names_in_code(paths=None) -> list[str]:
    out = []
    for doc in paths or docs():
        for line, body in fences(doc):
            for name, pattern in REMOVED_RE.items():
                if pattern.search(body):
                    out.append(f"{_rel(doc)}:{line} code block uses removed name {name}")
    return out


def test_the_extractor_actually_finds_the_documentation():
    """Verify documentation files, python fences, and relative links are discovered."""
    found = docs()
    assert len(found) >= 15, f"only found {len(found)} documents: {[_rel(d) for d in found]}"
    total_fences = sum(len(fences(d)) for d in found)
    assert total_fences >= 60, f"only found {total_fences} python fences"
    total_links = sum(
        1
        for d in found
        for m in LINK.finditer(d.read_text())
        if not m.group(1).startswith(("http://", "https://", "mailto:", "#"))
    )
    assert total_links >= 15, f"only found {total_links} relative links"


def test_every_python_fence_parses():
    bad = unparseable()
    assert not bad, (
        "these ```python blocks are not Python. If a block is a console "
        "transcript tag it ```pycon, if it is shell tag it ```bash, if it is "
        "output tag it ```text:\n  " + "\n  ".join(bad)
    )


def test_every_import_in_the_documentation_resolves():
    bad = unresolvable_imports()
    assert not bad, "documentation imports that do not resolve:\n  " + "\n  ".join(bad)


def test_every_relative_link_resolves():
    bad = broken_links()
    assert not bad, "documentation links to files that do not exist:\n  " + "\n  ".join(bad)


def test_no_code_block_uses_a_name_deleted():
    """Imports are not the only way a document names a dead class.

    `class APIService(CliffracerService, HTTPMixin)` imports nothing and
    resolves nothing, so the import check above is blind to it -- and that is
    the form most of the stale examples took.
    """
    bad = removed_names_in_code()
    assert not bad, "\n  ".join(bad)


# --- positive controls: each check must be able to fail --------------------


def test_CONTROL_a_bad_fence_is_detected(tmp_path: Path):
    doc = tmp_path / "bad.md"
    doc.write_text("```python\nthis is not python(\n```\n")
    assert unparseable([doc]), "a syntactically invalid fence was not detected"


def test_CONTROL_a_bad_import_is_detected(tmp_path: Path):
    doc = tmp_path / "bad.md"
    doc.write_text("```python\nfrom cliffracer import NoSuchName\nimport no_such_module\n```\n")
    found = unresolvable_imports([doc])
    assert len(found) == 2, f"expected both the bad module and the bad name, got {found}"


def test_CONTROL_a_bad_link_is_detected(tmp_path: Path):
    doc = tmp_path / "bad.md"
    doc.write_text("see [nothing](does/not/exist.md)\n")
    assert broken_links([doc]), "a link to a missing file was not detected"


def test_CONTROL_a_removed_name_is_detected(tmp_path: Path):
    doc = tmp_path / "bad.md"
    doc.write_text("```python\nclass S(CliffracerService, HTTPMixin):\n    pass\n```\n")
    assert removed_names_in_code([doc]), "a deleted class name in a code block was not detected"


def test_CONTROL_the_NATSService_pattern_does_not_match_its_own_suffixes():
    """The near miss, because a match alone proves nothing about a pattern.

    `NATSService` is a suffix of two other names on the list. A pattern without
    a left boundary flags every corrected `HttpExtension` example that still
    mentions HTTPNATSService in a comment, and -- worse -- reports the same
    line twice, which reads as two separate defects.
    """
    p = REMOVED_RE["NATSService"]
    assert p.search("svc = NATSService(config)"), "the pattern must match the real thing"
    assert not p.search("class S(HTTPNATSService):"), "matched its own suffix"
    assert not p.search("class S(WebSocketNATSService):"), "matched its own suffix"

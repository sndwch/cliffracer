"""Documentation that points a reader at code which is not there.

WHAT THIS IS FOR. Before the 1.0 docs pass every guide in this repository
taught classes that had been deleted: 56 imports in fenced blocks did not
resolve, 15 blocks tagged ```python were not Python, and 14 relative links led
nowhere. Not one of them was caught by anything, because nothing read the
documentation. A reader following the README's HTTP example got an ImportError
on line 1.

WHAT IT DOES NOT DO, stated so a green run is not over-read: it PARSES blocks,
RESOLVES imports, and resolves the free symbols each fence references through a
symbol table across every tracked document. It does not execute them, so a block
that binds the right names and then calls them wrongly passes here. Execution is
tests/repo/test_docs_handlers_would_start.py for documented services, and
tests/integration/test_examples_run.py for examples/; the extensions guide's
worked example is executed by tests/integration/test_extensions_guide.py. This
guard is the floor, not the ceiling.

Symbols a document may leave unbound are listed in EXEMPT_SYMBOLS by document
and symbol, each with a reason, and a test drops every entry in turn to check
the sweep would otherwise report it.

THE COUNT ASSERTIONS ARE NOT DECORATION. Every check here reports "0 problems"
when its extractor finds nothing at all -- a changed fence syntax, a moved
directory, a regex that stops matching. An all-zero result is what both success
and total failure look like, so each test also asserts it examined a plausible
number of things.
"""

import ast
import builtins
import importlib
import re
import subprocess
import symtable
from pathlib import Path
from urllib.parse import unquote

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# One `python` fence. Deliberately not `py`, `pycon`, `text` or `bash`: a
# console transcript is not source, and tagging one `python` is what made the
# original syntax-error count unreadable.
FENCE = re.compile(r"^```python[^\S\n]*\n(?P<source>.*?)^```[^\S\n]*$", re.S | re.M)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
# A fenced block of any language, and an inline code span: neither holds a link, whatever it looks
# like (`x[0](y)` is a subscript and a call). Both are blanked before links are read.
ANY_FENCE = re.compile(r"^(?P<mark>`{3,}|~{3,})[^\n]*\n.*?^(?P=mark)[^\S\n]*$", re.S | re.M)
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)[^\n]+?(?<!`)\1(?!`)")
# `[label]: target "title"`, the definition a reference-style link points at.
REF_DEFINITION = re.compile(r"^ {0,3}\[([^\]\n]+)\]:[^\S\n]*<?([^\s>]+)>?", re.M)
# `[text][label]` and `[label][]`; the empty label means the text is the label.
REF_USE = re.compile(r"\[([^\]\n]+)\]\[([^\]\n]*)\]")
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$", re.M)
HTML_ANCHOR = re.compile(r"""<a\s[^>]*?\b(?:id|name)=["']([^"']+)["']""", re.I)
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)

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
    """Return all tracked markdown files across the repository, excluding changelog."""
    if (REPO / ".git").exists():
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "*.md"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        found = [REPO / rel for rel in out]
    else:
        found = [
            p
            for p in REPO.rglob("*.md")
            if not any(part.startswith(".") and part != ".github" for part in p.parts)
            and ".venv" not in p.parts
            and ".agents" not in p.parts
        ]
    return sorted(d for d in found if d.name not in EXCLUDED)


def fences(doc: Path) -> list[tuple[int, str]]:
    """Return each Python fence's source and its first one-based document line."""
    text = doc.read_text()
    return [
        (text[: match.start("source")].count("\n") + 1, match.group("source"))
        for match in FENCE.finditer(text)
    ]


def markdown_line(source_line: int, relative_line: int = 1) -> int:
    """Translate a one-based line inside fence source to its document line."""
    return source_line + relative_line - 1


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)  # a control's temporary document


def unparseable(paths=None) -> list[str]:
    out = []
    for doc in paths or docs():
        for source_line, body in fences(doc):
            try:
                ast.parse(body)
            except SyntaxError as exc:
                out.append(f"{_rel(doc)}:{markdown_line(source_line, exc.lineno or 1)} {exc}")
    return out


def unresolvable_imports(paths=None) -> list[str]:
    out = []
    for doc in paths or docs():
        for source_line, body in fences(doc):
            try:
                tree = ast.parse(body)
            except SyntaxError:
                continue  # reported by the parse test; not double-counted here
            for node in ast.walk(tree):
                pairs: list[tuple[str, str | None]]
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
                        out.append(
                            f"{_rel(doc)}:{markdown_line(source_line, node.lineno)} "
                            f"import {module} -> {exc!r}"
                        )
                        continue
                    if attr is None or attr == "*" or hasattr(mod, attr):
                        continue
                    try:
                        importlib.import_module(f"{module}.{attr}")
                    except Exception:
                        out.append(
                            f"{_rel(doc)}:{markdown_line(source_line, node.lineno)} "
                            f"from {module} import {attr} -> not there"
                        )
    return out


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _without_fences(text: str) -> str:
    """The text with fenced blocks blanked, so every offset and line is unchanged."""
    return ANY_FENCE.sub(_blank, text)


def _prose(text: str) -> str:
    """The text with fenced blocks and code spans blanked: what a link can be written in."""
    return CODE_SPAN.sub(_blank, _without_fences(text))


def _slug(heading: str) -> str:
    """The id a renderer gives a heading: its text lowercased, punctuation dropped, spaces hyphens.

    A link keeps its text, code and emphasis marks go, and an underscore stays.
    """
    heading = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    heading = re.sub(r"[`*~]", "", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", heading).replace(" ", "-")


def anchors_of(doc: Path) -> set[str]:
    """What `#fragment` can name in a document: its headings, as ids, and its `id`/`name` anchors.

    A heading that repeats an earlier one takes `-1`, `-2`, ... after its id. Matching is by lower
    case, as a browser does it.
    """
    text = doc.read_text()
    found: set[str] = set()
    seen: dict[str, int] = {}
    for match in HEADING.finditer(_without_fences(text)):
        base = _slug(match.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    found.update(anchor.lower() for anchor in HTML_ANCHOR.findall(text))
    return found


def _problem_with(doc: Path, target: str) -> str | None:
    """Why a link target does not lead anywhere, or None when it does or is not ours to check."""
    if SCHEME.match(target) or target.startswith("<"):
        return None
    path, _, fragment = target.partition("#")
    destination = doc.parent / unquote(path) if path else doc
    if not destination.exists():
        return "no such file"
    if fragment and destination.is_file() and destination.suffix == ".md":
        if unquote(fragment).lower() not in anchors_of(destination):
            return f"no heading or anchor {fragment!r} in {_rel(destination)}"
    return None


def links_in(doc: Path) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """The link targets in a document's prose, and its reference-style links with no definition.

    Targets are `[text](target)` links and `[label]: target` definitions, each with its line.
    """
    prose = _prose(doc.read_text())

    def line(offset: int) -> int:
        return prose.count("\n", 0, offset) + 1

    targets = [(line(m.start(1)), m.group(1)) for m in LINK.finditer(prose)]
    defined = set()
    for m in REF_DEFINITION.finditer(prose):
        defined.add(m.group(1).strip().lower())
        targets.append((line(m.start(2)), m.group(2)))
    undefined = [
        (line(m.start()), m.group(0))
        for m in REF_USE.finditer(prose)
        if (m.group(2) or m.group(1)).strip().lower() not in defined
    ]
    return targets, undefined


def broken_links(paths=None) -> list[str]:
    """Links that lead nowhere: a missing file, a `#fragment` no heading or anchor in the target
    document carries, or a reference-style link whose `[label]: target` is not there."""
    out = []
    for doc in paths or docs():
        targets, undefined = links_in(doc)
        for number, target in targets:
            problem = _problem_with(doc, target)
            if problem:
                out.append(f"{_rel(doc)}:{number}: {target} ({problem})")
        for number, text in undefined:
            out.append(f"{_rel(doc)}:{number}: {text} (reference-style link with no definition)")
    return out


def removed_names_in_code(paths=None) -> list[str]:
    out = []
    for doc in paths or docs():
        for source_line, body in fences(doc):
            for name, pattern in REMOVED_RE.items():
                if pattern.search(body):
                    out.append(f"{_rel(doc)}:{source_line} code block uses removed name {name}")
    return out


def test_the_extractor_actually_finds_the_documentation():
    """Verify documentation files, python fences, and relative links are discovered."""
    found = docs()
    assert len(found) >= 30, f"only found {len(found)} documents: {[_rel(d) for d in found]}"
    total_fences = sum(len(fences(d)) for d in found)
    assert total_fences >= 88, f"only found {total_fences} python fences"
    relative = [t for d in found for _, t in links_in(d)[0] if not SCHEME.match(t)]
    assert len(relative) >= 30, f"only found {len(relative)} relative links"
    # The fragment half of a link is read too: a reader that dropped it would pass every one.
    assert sum("#" in t for t in relative) >= 5, "only found {} links with a #fragment".format(
        sum("#" in t for t in relative)
    )
    assert any(t.startswith("#") for t in relative), "no link to a heading of its own document"

    # Per-area fence floors guarantee individual documentation partitions remain inspected.
    readme_fences = len(fences(REPO / "README.md"))
    assert readme_fences >= 10, f"only found {readme_fences} python fences in README.md"

    quickstart_fences = len(fences(REPO / "QUICKSTART.md"))
    assert quickstart_fences >= 10, f"only found {quickstart_fences} python fences in QUICKSTART.md"

    docs_dir_fences = sum(len(fences(d)) for d in found if d.is_relative_to(REPO / "docs"))
    assert docs_dir_fences >= 33, f"only found {docs_dir_fences} python fences in docs/"

    examples_fences = sum(len(fences(d)) for d in found if d.is_relative_to(REPO / "examples"))
    assert examples_fences >= 8, f"only found {examples_fences} python fences in examples/"

    package_fences = sum(len(fences(d)) for d in found if d.is_relative_to(REPO / "packages"))
    assert package_fences >= 10, f"only found {package_fences} python fences in packages/"


def test_fence_coordinates_name_the_first_source_line_across_multiple_blocks(tmp_path: Path):
    doc = tmp_path / "coordinates.md"
    doc.write_text(
        "# Examples\n\n```python\nfirst = 1\n```\n\n## Later\n\n```python\n\n    second = 2\n```\n"
    )
    extracted = fences(doc)
    assert extracted == [(4, "first = 1\n"), (10, "\n    second = 2\n")]
    assert markdown_line(extracted[0][0]) == 4
    assert markdown_line(extracted[1][0], 2) == 11


ALT_PYTHON_FENCE = re.compile(r"^```(?:py|python3)\s*$", re.M)


def test_no_document_uses_alternate_python_fence_tags():
    """Ensure python code blocks are consistently tagged ```python for static analysis."""
    offenders = []
    for doc in docs():
        for number, line in enumerate(doc.read_text().splitlines(), 1):
            if ALT_PYTHON_FENCE.match(line):
                offenders.append(f"{_rel(doc)}:{number} {line}")
    assert not offenders, (
        "code blocks must be tagged ```python for static analysis:\n  " + "\n  ".join(offenders)
    )


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


BUILTIN_NAMES = set(dir(builtins))
FRAMEWORK_SYMBOLS = set(dir(importlib.import_module("cliffracer")))
for _pkg in (
    "cliffracer_metrics",
    "cliffracer_otel",
    "cliffracer_auth",
    "cliffracer_resilience",
    "cliffracer_cron",
    "cliffracer_kv",
):
    try:
        _mod = importlib.import_module(_pkg)
        FRAMEWORK_SYMBOLS |= {k for k in dir(_mod) if not k.startswith("_")}
    except ImportError:
        pass

SNIPPET_CONTEXT_NAMES = {"self", "config", "args", "kwargs", "__name__"}


def _walk_table(table: symtable.SymbolTable, known: set[str]) -> set[str]:
    res: set[str] = set()
    for s in table.get_symbols():
        if s.is_global():
            n = s.get_name()
            if (
                n not in BUILTIN_NAMES
                and n not in FRAMEWORK_SYMBOLS
                and n not in known
                and n not in SNIPPET_CONTEXT_NAMES
            ):
                res.add(n)
    for child in table.get_children():
        res |= _walk_table(child, known)
    return res


# Symbols a document may leave unbound, and why. Keyed by document and symbol
# rather than by line: a fence that moves keeps its exemption, and an exemption
# can never drift onto a statement it was not written for.
EXEMPT_SYMBOLS: dict[str, dict[str, str]] = {
    "CONTRIBUTING.md": {
        "emoji_lines": "names the sweep a contributor writes, shown as a fragment",
        "fixture_with_an_emoji": "stand-in fixture name in a worked example of the sweep",
        "fixture_with_only_prose": "stand-in fixture name in a worked example of the sweep",
    },
    "docs/api-reference.md": {
        "Order": "request model the reader supplies; the fence documents the call shape",
        "Receipt": "reply model the reader supplies; the fence documents the call shape",
    },
    "examples/correlation/README.md": {
        "logger": "the service's own logger, bound on the instance the fragment runs in",
    },
    "examples/timer/README.md": {
        "service": "the constructed service the fragment operates on",
    },
}


def unresolvable_symbols_in_document(doc: Path, exempt: set[str] | None = None) -> list[str]:
    """Find unresolved free/global symbols across python code blocks in a document.

    `exempt` overrides the document's entry in EXEMPT_SYMBOLS, which is what
    lets a test drop one exemption and check that it was doing something.
    """
    if exempt is None:
        exempt = set(EXEMPT_SYMBOLS.get(_rel(doc), {}))
    doc_defined: set[str] = set()
    problems: list[str] = []
    for source_line, source in fences(doc):
        try:
            tbl = symtable.symtable(source, str(doc), "exec")
        except SyntaxError:
            continue

        block_defined: set[str] = set()
        for sym in tbl.get_symbols():
            if sym.is_imported() or sym.is_assigned():
                block_defined.add(sym.get_name())

        known = block_defined | doc_defined
        unbound: set[str] = set()
        for sym in tbl.get_symbols():
            if not sym.is_imported() and not sym.is_assigned():
                name = sym.get_name()
                if (
                    name not in BUILTIN_NAMES
                    and name not in FRAMEWORK_SYMBOLS
                    and name not in known
                    and name not in SNIPPET_CONTEXT_NAMES
                ):
                    unbound.add(name)

        for child in tbl.get_children():
            unbound |= _walk_table(child, known)

        doc_defined |= block_defined
        unbound -= exempt
        if unbound:
            problems.append(f"{_rel(doc)}:{source_line} unresolved symbols: {sorted(unbound)}")
    return problems


def test_every_document_resolves_the_symbols_its_fences_reference():
    """Every tracked document, not only the two front-page ones.

    A fence that imports cleanly and then references a name it never binds is
    the defect this tier exists to catch, and it is as likely in a package
    README as in the front page.
    """
    offenders: list[str] = []
    for doc in docs():
        offenders.extend(unresolvable_symbols_in_document(doc))
    assert not offenders, (
        "documented python code blocks reference unbound symbols:\n  " + "\n  ".join(offenders)
    )


def test_the_symbol_sweep_reads_every_tracked_document():
    """A sweep that reached two files would pass the check above."""
    scanned = docs()
    assert len(scanned) > 20, (
        f"only {len(scanned)} documents scanned; the sweep is not reading the tree"
    )
    rels = {_rel(d) for d in scanned}
    assert "README.md" in rels
    assert any(r.startswith("packages/") for r in rels), "packages/ documents not reached"


def test_the_exemptions_all_name_a_tracked_document():
    """An exemption on a path that no longer exists protects nothing."""
    rels = {_rel(d) for d in docs()}
    missing = sorted(set(EXEMPT_SYMBOLS) - rels)
    assert missing == [], f"exemptions name documents that are not tracked: {missing}"


def test_every_exemption_is_load_bearing():
    """Each exempted symbol must be one the sweep would otherwise report.

    Without this, an exemption outlives the fence that needed it and quietly
    widens what the sweep will not look at.
    """
    dead: list[str] = []
    for rel, symbols in EXEMPT_SYMBOLS.items():
        doc = REPO / rel
        for symbol in symbols:
            without = set(symbols) - {symbol}
            findings = unresolvable_symbols_in_document(doc, exempt=without)
            if not any(f"'{symbol}'" in f for f in findings):
                dead.append(f"{rel}:{symbol}")
    assert dead == [], (
        "these exemptions are not doing anything; the document no longer leaves "
        f"the symbol unbound: {dead}"
    )


def test_the_exemptions_all_carry_a_reason():
    empty = [
        f"{rel}:{sym}"
        for rel, symbols in EXEMPT_SYMBOLS.items()
        for sym, reason in symbols.items()
        if not reason.strip()
    ]
    assert empty == [], f"exemptions without a stated reason: {empty}"


# --- positive controls: each check must be able to fail --------------------


def test_CONTROL_a_bad_fence_is_detected(tmp_path: Path):
    doc = tmp_path / "bad.md"
    doc.write_text("```python\nthis is not python(\n```\n")
    assert unparseable([doc]), "a syntactically invalid fence was not detected"


def test_a_syntax_failure_reports_its_document_line(tmp_path: Path):
    doc = tmp_path / "bad.md"
    doc.write_text("# Example\n\n```python\n\nvalue = (\n```\n")
    assert unparseable([doc])[0].startswith(f"{doc}:5 ")


def test_CONTROL_a_bad_import_is_detected(tmp_path: Path):
    doc = tmp_path / "bad.md"
    doc.write_text("```python\nfrom cliffracer import NoSuchName\nimport no_such_module\n```\n")
    found = unresolvable_imports([doc])
    assert len(found) == 2, f"expected both the bad module and the bad name, got {found}"


def test_import_failures_report_their_own_document_lines(tmp_path: Path):
    doc = tmp_path / "bad.md"
    doc.write_text("```python\nfrom cliffracer import NoSuchName\n\nimport no_such_module\n```\n")
    found = unresolvable_imports([doc])
    assert found[0].startswith(f"{doc}:2 ")
    assert found[1].startswith(f"{doc}:4 ")


def test_CONTROL_a_bad_link_is_detected(tmp_path: Path):
    doc = tmp_path / "bad.md"
    doc.write_text("see [nothing](does/not/exist.md)\n")
    assert broken_links([doc]), "a link to a missing file was not detected"


def _doc(tmp_path: Path, name: str, text: str) -> Path:
    doc = tmp_path / name
    doc.write_text(text)
    return doc


def test_CONTROL_an_anchor_no_heading_carries_is_detected(tmp_path: Path):
    """A fragment is checked against the headings of the document it names: its own, or another."""
    other = _doc(tmp_path, "other.md", "# Other\n\n## Two Words\n")
    doc = _doc(
        tmp_path,
        "doc.md",
        "# Real\n\n[ok](#real) [ok](other.md#two-words) [bad](#nope) [bad](other.md#two)\n",
    )

    found = broken_links([doc, other])

    assert len(found) == 2, found
    assert any(":3: #nope " in f and "no heading or anchor" in f for f in found), found
    assert any(":3: other.md#two " in f for f in found), found


def test_the_id_a_heading_gets_follows_the_renderer(tmp_path: Path):
    """Code and emphasis marks drop out, a link keeps its text, punctuation goes and an underscore
    stays; a repeated heading takes -1, -2; an explicit anchor is a target too."""
    doc = _doc(
        tmp_path,
        "doc.md",
        "# `ServiceConfig` options: the *basics*\n\n## See [the guide](x.md) for snake_case\n\n"
        '## Same\n\n## Same\n\n### Same ###\n\n<a id="custom"></a>\n',
    )

    assert anchors_of(doc) == {
        "serviceconfig-options-the-basics",
        "see-the-guide-for-snake_case",
        "same",
        "same-1",
        "same-2",
        "custom",
    }


def test_CONTROL_a_heading_in_a_code_block_is_not_an_anchor(tmp_path: Path):
    doc = _doc(tmp_path, "doc.md", "# Real\n\n```bash\n# not a heading\n```\n")

    assert anchors_of(doc) == {"real"}


def test_the_fragment_is_matched_the_way_a_browser_does(tmp_path: Path):
    """Case does not matter and a percent-encoded fragment is decoded. A fragment on a file that
    is not markdown is not ours to check, and one on a directory is only a directory."""
    _doc(tmp_path, "code.py", "x = 1\n")
    doc = _doc(
        tmp_path,
        "doc.md",
        "# Two Words\n\n[a](#TWO-WORDS) [b](#two%2Dwords) [c](code.py#L3) [d](.#anything)\n",
    )

    assert broken_links([doc]) == []


def test_CONTROL_a_reference_style_link_is_read(tmp_path: Path):
    """The target of `[label]: target` is checked like an inline link, and a `[text][label]` with
    no definition is reported, whichever case the label is written in."""
    _doc(tmp_path, "there.md", "# There\n")
    doc = _doc(
        tmp_path,
        "doc.md",
        "[fine][a] and [Also Fine][] and [gone][b] and [orphan][c] and [empty][]\n\n"
        "[a]: there.md#there\n"
        "[ALSO FINE]: there.md\n"
        "[b]: missing.md\n"
        '[d]: there.md#nowhere "a title"\n',
    )

    found = broken_links([doc])

    assert len(found) == 4, found
    assert any(":5: missing.md (no such file)" in f for f in found), found
    assert any(":6: there.md#nowhere " in f for f in found), found
    assert any("[orphan][c] (reference-style link with no definition)" in f for f in found), found
    assert any("[empty][] (reference-style link with no definition)" in f for f in found), found


def test_CONTROL_what_looks_like_a_link_in_code_is_not_one(tmp_path: Path):
    """A subscript and a call in a fence, in a span, or a heading in a fence: none is a link, and
    the line numbers of what follows are unchanged by blanking them."""
    doc = _doc(
        tmp_path,
        "doc.md",
        '```python\nx = rows[0](missing)\ny = d["a"]["b"]\n```\n\n'
        'Use `d["a"]["b"]` or `f[0](g)`.\n\n[bad](missing.md)\n',
    )

    assert broken_links([doc]) == [f"{doc}:8: missing.md (no such file)"]


def test_CONTROL_a_scheme_is_not_a_file(tmp_path: Path):
    doc = _doc(
        tmp_path, "doc.md", "[a](https://x.invalid/y#z) [b](mailto:a@b.invalid) [c](tel:1)\n"
    )

    assert broken_links([doc]) == []


def test_CONTROL_a_removed_name_is_detected(tmp_path: Path):
    doc = tmp_path / "bad.md"
    doc.write_text("```python\nclass S(CliffracerService, HTTPMixin):\n    pass\n```\n")
    assert removed_names_in_code([doc]), "a deleted class name in a code block was not detected"


def test_CONTROL_the_NATSService_pattern_does_not_match_its_own_suffixes():
    """The near miss, because a match alone proves nothing about a pattern.

    `NATSService` is a suffix of two other names on the list. A pattern without
    a left boundary flags every corrected `CliffracerService` example that still
    mentions HTTPNATSService in a comment, and -- worse -- reports the same
    line twice, which reads as two separate defects.
    """
    p = REMOVED_RE["NATSService"]
    assert p.search("svc = NATSService(config)"), "the pattern must match the real thing"
    assert not p.search("class S(HTTPNATSService):"), "matched its own suffix"
    assert not p.search("class S(WebSocketNATSService):"), "matched its own suffix"


def test_CONTROL_unbound_symbol_in_code_block_is_detected(tmp_path: Path):
    """Control: an undefined variable inside a python fence is detected."""
    doc = tmp_path / "example.md"
    doc.write_text(
        "```python\n"
        "from cliffracer import CliffracerService\n\n"
        "class Orders(CliffracerService):\n"
        "    token = UNRESOLVED_TOKEN_NAME\n"
        "```\n"
    )
    problems = unresolvable_symbols_in_document(doc)
    assert problems and any("UNRESOLVED_TOKEN_NAME" in p for p in problems)

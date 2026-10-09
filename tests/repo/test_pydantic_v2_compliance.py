"""Guards against Pydantic v1-era APIs, which Pydantic V3 removes.

These constructs still work under Pydantic 2.x but emit deprecation warnings,
so this suite fails on the warning rather than waiting for the V3 upgrade to
break the library.
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
CORE_SRC = REPO / "src" / "cliffracer"
PACKAGE_SRCS = sorted(p for p in REPO.glob("packages/*/src") if p.is_dir())
ROOTS = [CORE_SRC] + PACKAGE_SRCS


def test_scan_roots_cover_all_workspace_packages():
    """Floor check: ensure every workspace member package is included in the scan roots."""
    packages_pyproject = list(REPO.glob("packages/*/pyproject.toml"))
    assert len(ROOTS) == len(packages_pyproject) + 1, (
        f"Expected {len(packages_pyproject) + 1} roots (core + {len(packages_pyproject)} packages), "
        f"found {len(ROOTS)}: {ROOTS}"
    )
    assert len(ROOTS) >= 8, (
        f"Floor check failed: expected at least 8 source roots, found {len(ROOTS)}"
    )


def _scan(matches, roots: list[Path] | None = None) -> list[str]:
    """Return offending lines as ``path:line: source`` across all inspected roots."""
    hits: list[str] = []
    target_roots = roots if roots is not None else ROOTS
    for base in target_roots:
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path.relative_to(base)
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if matches(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def _report(offenders: list[str]) -> str:
    """One offender per line, indented, so a multi-hit failure stays readable."""
    return "Found at:\n" + "\n".join(f"  {o}" for o in offenders)


def v1_validator_sites(path: Path) -> list[str]:
    """Every place this file uses Pydantic v1's ``validator``, read from the AST.

    Three shapes, and they are the three that make a file actually depend on it:
    importing the name from pydantic, reaching it through the module, and
    decorating with a bare ``validator``.

    READ AS CODE, NOT AS TEXT. This was a line scan for the bare word
    ``validator`` with the v2 spellings stripped out, which matched ordinary
    English -- its own docstring, an assertion message, and a package called
    ``email-validator`` (a hyphen is not a word character). It flagged a
    docstring in a pull request that changed nothing about pydantic. A guard that
    reds on correct prose gets weakened by the next person who meets it, and the
    rule underneath is worth keeping: @validator really is removed in V3.

    It also settles a tension no line-at-a-time rule can. The old reader did see

        from pydantic import (
            validator,
        )

    but only by matching `validator` alone on its own line -- the same looseness
    that read prose. Tighten such a rule to require both words together and it
    stops seeing that form; leave it loose and it keeps flagging English. The
    AST has both properties at once.
    """
    found: list[str] = []
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - a syntax error fails the suite elsewhere
        raise AssertionError(f"{path} could not be parsed, so it was not checked: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pydantic":
            for alias in node.names:
                if alias.name == "validator":
                    found.append(f"{node.lineno}: from {node.module} import validator")
        elif isinstance(node, ast.Attribute) and node.attr == "validator":
            if isinstance(node.value, ast.Name) and node.value.id == "pydantic":
                found.append(f"{node.lineno}: pydantic.validator")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Name) and target.id == "validator":
                    found.append(f"{decorator.lineno}: @validator on {node.name}")
    return found


def _scan_v1_validator(roots: list[Path] | None = None) -> tuple[list[str], int]:
    """Offending sites, and how many files were read to find them.

    The file count is returned because "no offenders" and "read nothing" are the
    same empty list.
    """
    hits: list[str] = []
    read = 0
    for base in roots if roots is not None else ROOTS:
        for path in sorted(base.rglob("*.py")):
            read += 1
            rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path
            hits.extend(f"{rel}:{site}" for site in v1_validator_sites(path))
    return hits, read


def test_no_class_based_config_in_library_code():
    offenders = _scan(lambda line: "class Config:" in line)
    assert offenders == [], (
        "class-based Config is removed in Pydantic V3; use "
        "model_config = ConfigDict(...) instead. " + _report(offenders)
    )


def test_no_legacy_config_keys_in_library_code():
    offenders = _scan(
        lambda line: bool(re.search(r"\b(allow_mutation|orm_mode)\b", line))
        and not line.strip().startswith("#")
    )
    assert offenders == [], "allow_mutation and orm_mode are removed in Pydantic V3. " + _report(
        offenders
    )


def test_no_v1_validator_import_in_library_code():
    offenders, files_read = _scan_v1_validator()

    assert files_read > 0, (
        f"the sweep read no files, so an empty offender list means nothing; roots were {ROOTS}"
    )
    assert offenders == [], (
        "@validator is removed in Pydantic V3; use @field_validator. " + _report(offenders)
    )


def test_CONTROL_a_real_v1_validator_is_caught(tmp_path: Path):
    """Each of the three shapes, so the narrowing did not narrow to nothing."""
    single = tmp_path / "single.py"
    single.write_text("from pydantic import BaseModel, validator\n")

    multi = tmp_path / "multi.py"
    multi.write_text("from pydantic import (\n    BaseModel,\n    validator,\n)\n")

    decorated = tmp_path / "decorated.py"
    decorated.write_text(
        "from pydantic import BaseModel\n\n\n"
        "class M(BaseModel):\n"
        "    @validator('x')\n"
        "    def check(cls, v):\n"
        "        return v\n"
    )

    attribute = tmp_path / "attribute.py"
    attribute.write_text("import pydantic\n\nv = pydantic.validator\n")

    for path, expected in (
        (single, "import validator"),
        (multi, "import validator"),
        (decorated, "@validator on check"),  # the method, not the class
        (attribute, "pydantic.validator"),
    ):
        sites = v1_validator_sites(path)
        assert sites, f"{path.name} was not flagged"
        assert any(expected in site for site in sites), (path.name, sites, expected)


def test_CONTROL_a_tightened_line_reader_would_miss_the_multiline_import(tmp_path: Path):
    """Why the fix is the AST and not a tighter regex.

    The bare-word reader did catch this form, but only by matching `validator`
    alone on its own line -- the same looseness that flagged prose. So the two
    properties pull against each other for any line-at-a-time rule: tighten it
    to require `pydantic` and `validator` together and it stops seeing a
    parenthesised import; leave it loose and it reads English.

    Asserted from both sides: no line of this fixture carries both words, and
    the AST reader sees it anyway.
    """
    multi = tmp_path / "multi.py"
    multi.write_text("from pydantic import (\n    BaseModel,\n    validator,\n)\n")

    lines = multi.read_text().splitlines()

    assert not any("pydantic" in line and "validator" in line for line in lines), (
        "this fixture no longer splits the import across lines, so it stops "
        "demonstrating what a tightened line reader would miss"
    )
    assert v1_validator_sites(multi), "the AST reader must still see it"


def test_CONTROL_prose_about_validation_is_not_a_hit():
    """The false positives that prompted this, asserted against the real tree.

    `tests/` is not in `ROOTS`, so these lines are invisible to the guard today
    -- but scan roots here get widened deliberately, and on the day this one's
    are, seven correct lines must not red. Measured at 3f188f3: 7 lines match a
    bare-word reading of `validator`, 0 are real uses.
    """
    offenders, files_read = _scan_v1_validator([REPO / "tests"])

    assert files_read > 0, "the sweep read no test files"
    assert offenders == [], (
        "prose about validation is being read as a Pydantic v1 import:\n  " + "\n  ".join(offenders)
    )


def test_CONTROL_a_hyphenated_package_name_is_not_a_hit(tmp_path: Path):
    """`email-validator` is a real dependency of this project and not a v1 import.

    A hyphen is not a word character, so `\bvalidator\b` matched inside the
    package's name. Kept as its own case because it is the least obvious of the
    false positives and the easiest to reintroduce.
    """
    path = tmp_path / "deps.py"
    path.write_text(
        'REQUIRED = ["aiohttp", "email-validator"]\nassert "email-validator" not in BLOCK\n'
    )

    assert v1_validator_sites(path) == []


def test_no_dunder_fields_in_library_code():
    offenders = _scan(lambda line: "__fields__" in line)
    assert offenders == [], (
        "__fields__ is removed in Pydantic V3; use model_fields instead. " + _report(offenders)
    )


def test_no_root_validator_in_library_code():
    offenders = _scan(lambda line: "@root_validator" in line)
    assert offenders == [], (
        "@root_validator is removed in Pydantic V3; use @model_validator instead. "
        "" + _report(offenders)
    )


def test_no_parse_obj_in_library_code():
    offenders = _scan(lambda line: "parse_obj(" in line)
    assert offenders == [], (
        "parse_obj( is removed in Pydantic V3; use model_validate instead. " + _report(offenders)
    )


def test_no_parse_raw_in_library_code():
    offenders = _scan(lambda line: "parse_raw(" in line)
    assert offenders == [], (
        "parse_raw( is removed in Pydantic V3; use model_validate_json instead. "
        "" + _report(offenders)
    )


_PROBE_MARKER = "PYDANTIC_WARNINGS_JSON:"

_IMPORT_PROBE = f"""\
import importlib
import json
import sys
import warnings

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    importlib.import_module(sys.argv[1])

messages = [str(w.message) for w in caught if "Pydantic" in type(w.message).__name__]
print("{_PROBE_MARKER}" + json.dumps(messages))
"""


# Hand-listed, because importing each in its own interpreter costs a process
# apiece. test_the_probed_modules_reach_every_scanned_package keeps the list
# honest: a new member package has to be represented here.
PROBED_MODULES = [
    "cliffracer.core.service_config",
    "cliffracer.core.validation",
    "cliffracer.core.messages",
    "cliffracer_kv.serialization",
    "cliffracer_auth.simple_auth",
    "cliffracer_cron.cron",
    "cliffracer_cyanide.config",
    "cliffracer_dlq.records",
    "cliffracer_logging.config",
    "cliffracer_metrics.batch_processor",
    "cliffracer_otel.extension",
    "cliffracer_resilience.rate_limiter",
]


def _declared_at(name: str) -> str:
    """`path:line` of a module-level assignment in this file.

    Computed rather than written down, so the instruction in a failure message
    cannot drift from where the list actually is.
    """
    path = Path(__file__)
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith(f"{name} ="):
            return f"{path.relative_to(REPO)}:{number}"
    return str(path.relative_to(REPO))  # pragma: no cover - the name always exists


def _package_of(module: str) -> str:
    return module.split(".", 1)[0]


def test_the_probed_modules_reach_every_scanned_package():
    """Every root the scan covers must have a module in the import probe.

    The scan roots are derived from the tree, so a new member package joins
    them automatically. This list is not, so without this check a new package
    would be swept for the v1 spellings and never imported under a warning
    filter -- the half that catches a deprecation the text scan cannot see.
    """
    scanned = {CORE_SRC.name if r == CORE_SRC else r.parent.name.replace("-", "_") for r in ROOTS}
    probed = {_package_of(m) for m in PROBED_MODULES}
    missing = sorted(scanned - probed)
    assert missing == [], (
        f"these scanned packages have no module in the import probe: {missing}. "
        f"Add one module from each to {_declared_at('PROBED_MODULES')}, or the scan "
        f"reads their text without ever importing them. Pick a module that actually "
        f"defines models or settings; the list is curated for that reason and is not "
        f"derived from the tree."
    )


@pytest.mark.parametrize("module", PROBED_MODULES)
def test_importing_module_emits_no_pydantic_deprecation(module):
    """Import the module in a fresh interpreter and fail on any Pydantic deprecation.

    The subprocess gives a genuine first import. An in-process reload rebinds
    module-level singletons - the ContextVar in cliffracer_auth.simple_auth is
    one - so every importer holding the original object reads a different
    variable for the rest of the session.
    """
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE, module],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, (
        f"importing {module} in a fresh interpreter failed:\n{result.stderr}"
    )
    marked = [line for line in result.stdout.splitlines() if line.startswith(_PROBE_MARKER)]
    assert len(marked) == 1, (
        f"probe for {module} did not report exactly one result line; stdout was:\n{result.stdout}"
    )
    pydantic_warnings = json.loads(marked[0][len(_PROBE_MARKER) :])
    assert pydantic_warnings == [], f"{module} emits: {pydantic_warnings}"


# The v1 model APIs Pydantic V3 removes, beyond the ones each test above names.
# A line-level scan cannot see what a name is bound to, so two of these match
# correct code as well: `ValidationError.json()` is a pydantic_core method and
# is not the deprecated `BaseModel.json()`, and `.copy()` on a dict is not
# `BaseModel.copy()`. Those lines are exempted individually, by the text that
# makes them safe rather than by line number, and
# test_every_retired_spelling_exemption_is_load_bearing fails on one that stops
# matching.
RETIRED_SPELLINGS: dict[str, str] = {
    ".dict(": "use model_dump()",
    ".json(": "use model_dump_json()",
    ".copy(": "use model_copy()",
    "from_orm": "use model_validate() with from_attributes",
    "update_forward_refs": "use model_rebuild()",
    "__fields_set__": "use model_fields_set",
    "min_items": "use min_length",
    "const=": "use Literal[...] for a fixed value",
}

# spelling -> (path suffix, the text that makes this line correct, why)
RETIRED_SPELLING_EXEMPTIONS: dict[str, list[tuple[str, str, str]]] = {
    ".json(": [
        (
            "core/dispatch/dlq.py",
            "json.loads(error.json())",
            "error is a pydantic_core ValidationError, whose json() is current API "
            "rather than the deprecated BaseModel.json().",
        ),
        (
            "core/dispatch/rpc.py",
            "json.loads(error.json())",
            "the same ValidationError method, rendering the failure into the reply.",
        ),
    ],
    ".copy(": [
        (
            "cliffracer_metrics/batch_processor.py",
            "self.stats.copy()",
            "a dict copy of the stats mapping, not BaseModel.copy().",
        ),
        (
            "cliffracer_metrics/metrics.py",
            "self._connection_stats.copy()",
            "a dict copy of the connection stats, not BaseModel.copy().",
        ),
        (
            "cliffracer_metrics/metrics.py",
            "self._custom_metrics.copy()",
            "a dict copy of the custom metrics, not BaseModel.copy().",
        ),
    ],
}


def _exempt(spelling: str, hit: str) -> bool:
    """Whether one `path:line: source` hit is a declared safe use."""
    for path_suffix, marker, _reason in RETIRED_SPELLING_EXEMPTIONS.get(spelling, []):
        location, _, source = hit.partition(": ")
        if path_suffix in location and marker in source:
            return True
    return False


def retired_spelling_hits(spelling: str) -> list[str]:
    """Offending lines for one spelling, with the declared safe uses removed."""
    return [h for h in _scan(lambda line: spelling in line) if not _exempt(spelling, h)]


@pytest.mark.parametrize("spelling", sorted(RETIRED_SPELLINGS))
def test_no_retired_model_api_in_library_code(spelling: str):
    offenders = retired_spelling_hits(spelling)
    assert offenders == [], (
        f"{spelling} is a Pydantic v1 model API that V3 removes; "
        f"{RETIRED_SPELLINGS[spelling]}. " + _report(offenders)
    )


@pytest.mark.parametrize("spelling", sorted(RETIRED_SPELLINGS))
def test_CONTROL_each_retired_spelling_is_caught(spelling: str, tmp_path: Path):
    """Every spelling must be found where it really appears.

    Written per spelling rather than once over the set, so a matcher that
    silently stopped finding one of them fails on that one by name.
    """
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "mod.py").write_text(f"value = obj{spelling}arg)\n")
    found = _scan(lambda line: spelling in line, roots=[src])
    assert len(found) == 1, f"{spelling} not found in {src}: {found}"


def test_CONTROL_a_safe_use_is_only_skipped_where_it_is_declared(tmp_path: Path):
    """An exemption is a location and a text, not a blanket pass for the name.

    The same `error.json()` call in a file nobody exempted must still be
    reported, or the exemption would excuse the spelling everywhere.
    """
    src = tmp_path / "elsewhere"
    src.mkdir()
    (src / "mod.py").write_text("errors = json.loads(error.json())\n")
    found = [
        h for h in _scan(lambda line: ".json(" in line, roots=[src]) if not _exempt(".json(", h)
    ]
    assert len(found) == 1, found


def test_every_retired_spelling_exemption_is_load_bearing():
    """Each declared safe use must be a line the scan would otherwise report.

    An exemption whose line has been rewritten excuses nothing and hides the
    next occurrence in that file.
    """
    dead: list[str] = []
    for spelling, entries in RETIRED_SPELLING_EXEMPTIONS.items():
        raw = _scan(lambda line: spelling in line)  # noqa: B023
        for path_suffix, marker, reason in entries:
            assert reason.strip(), f"{path_suffix} {marker} has no stated reason"
            if not any(path_suffix in h.partition(": ")[0] and marker in h for h in raw):
                dead.append(f"{spelling} {path_suffix} {marker!r}")
    assert dead == [], f"exemptions that match nothing in the source: {dead}"


def test_datetime_still_serializes_to_json():
    """Verify datetime serializes to JSON string natively in Pydantic v2."""
    from cliffracer.core.messages import Message

    payload = json.loads(Message().model_dump_json())
    assert isinstance(payload["timestamp"], str)
    assert "T" in payload["timestamp"], "datetime should be ISO-8601"


def test_CONTROL_scan_finds_violation_in_custom_root(tmp_path: Path):
    """Control: Verify _scan finds class Config violations in provided roots."""
    pkg_src = tmp_path / "packages" / "cliffracer-test" / "src" / "test_pkg"
    pkg_src.mkdir(parents=True)
    (pkg_src / "mod.py").write_text("class Config:\n    allow_mutation = False\n")
    found = _scan(lambda line: "class Config:" in line, roots=[pkg_src])
    assert len(found) == 1
    assert "class Config:" in found[0]

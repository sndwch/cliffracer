"""Guards against Pydantic v1-era APIs, which Pydantic V3 removes.

These constructs still work under Pydantic 2.x but emit deprecation warnings,
so this suite fails on the warning rather than waiting for the V3 upgrade to
break the library.
"""

import importlib
import re
import warnings
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "cliffracer"


def _scan(matches, root: Path | None = None) -> list[str]:
    """Return offending lines as ``path:line: source``.

    These matchers are deliberately crude — for a compliance guard a false
    positive is far cheaper than a false negative — so false positives are an
    expected cost rather than an aberration, and the matching below is left
    alone. The entire expense is in DIAGNOSING one.

    Reporting only a filename, while asserting a construct that may appear
    nowhere in it, leads the reader to assume the check is right and that they
    simply cannot see the problem. On a thousand-line module that is minutes
    spent looking for something that is not there. The line and its number turn
    that into a glance.
    """
    hits: list[str] = []
    base = root or SRC
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(base)
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if matches(line):
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def _report(offenders: list[str]) -> str:
    """One offender per line, indented, so a multi-hit failure stays readable."""
    return "Found at:\n" + "\n".join(f"  {o}" for o in offenders)


def _v1_validator(line: str) -> bool:
    """True if this line references Pydantic v1's ``validator``.

    A naive ``"import validator" in line`` check has a false negative: a line
    like ``from pydantic import field_validator, validator`` becomes
    ``from pydantic import , validator`` once the known-good spelling is
    stripped, matching neither "import validator" nor "@validator(". So look
    for a bare "validator" token not preceded by the field_/model_/root_
    prefixes or a dotted path, and separately catch the fully-qualified
    ``@pydantic.validator(`` form the word-boundary regex deliberately excludes.
    """
    stripped = line
    for spelling in ("field_validator", "model_validator", "root_validator"):
        stripped = stripped.replace(spelling, "")
    return bool(re.search(r"(?<![\w.])validator\b", stripped)) or ("@pydantic.validator(" in line)


def test_no_class_based_config_in_library_code():
    offenders = _scan(lambda line: "class Config:" in line)
    assert offenders == [], (
        "class-based Config is removed in Pydantic V3; use "
        "model_config = ConfigDict(...) instead. " + _report(offenders)
    )


def test_no_v1_validator_import_in_library_code():
    offenders = _scan(_v1_validator)
    assert offenders == [], (
        "@validator is removed in Pydantic V3; use @field_validator. " + _report(offenders)
    )


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


@pytest.mark.parametrize(
    "module",
    [
        "cliffracer.core.service_config",
        "cliffracer.core.validation",
        "cliffracer.core.messages",
    ],
)
def test_importing_module_emits_no_pydantic_deprecation(module):
    importlib.invalidate_caches()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mod = importlib.import_module(module)
        importlib.reload(mod)
    pydantic_warnings = [str(w.message) for w in caught if "Pydantic" in type(w.message).__name__]
    assert pydantic_warnings == [], f"{module} emits: {pydantic_warnings}"


def test_datetime_still_serializes_to_json():
    """Verify datetime serializes to JSON string natively in Pydantic v2."""
    import json

    from cliffracer.core.messages import Message

    payload = json.loads(Message().model_dump_json())
    assert isinstance(payload["timestamp"], str)
    assert "T" in payload["timestamp"], "datetime should be ISO-8601"

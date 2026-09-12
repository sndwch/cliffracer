"""The suite's own naming and marker rules, enforced against the suite.

Three rules:

1. Every test module declares exactly one tier marker, once, at module level,
   and the tier matches the directory it sits in. A module with no tier is
   invisible to `-m unit` and `-m integration` alike, and reports nothing.
2. A filename does not repeat the tag of the directory holding it. A tag that
   only some files in a directory carry says nothing either way.
3. A qualifier token sits at the END of a filename, so that a name sorts and
   globs by its subject.

The checks read the tree, not a rendered artifact: rule 1 parses each module's
AST rather than asking pytest, which is also what makes it enforce the "declare
it once, at module level" half of the rule.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# The tier each directory's tests must declare.
TIERS = {
    "tests/unit": "unit",
    "tests/repo": "repo",
    "tests/transport": "unit",
    "tests/integration": "integration",
    "tests/benchmark": "benchmark",
}

# Tag a directory must not see repeated in its files' names.
DIRECTORY_TAGS = {
    "tests/integration": ("integration",),
    "tests/transport": ("transport", "e2e"),
    "tests/benchmark": ("benchmark",),
    "tests/repo": ("repo",),
}

# Tokens that qualify a subject and therefore belong at the end of the name.
QUALIFIERS = ("adversarial", "stress")

ALL_TIERS = set(TIERS.values())


def all_test_modules() -> list[Path]:
    """Every test module in the tree, tests/ and packages/ alike."""
    found = []
    for base in ("tests", "packages"):
        found += [
            p
            for p in (REPO / base).rglob("test_*.py")
            if "__pycache__" not in p.parts and "fixtures" not in p.parts
        ]
    return sorted(found)


def _rel(path: Path) -> str:
    """Path from the nearest `tests`/`packages` anchor, wherever the file lives.

    Not `relative_to(REPO)`: the CONTROL tests below build the same directory
    shapes under tmp_path, and a check that only worked on real repo paths
    could not be falsified.
    """
    parts = path.parts
    # `packages` first, and its outermost occurrence: a package's own tests live
    # at packages/<name>/tests/, so anchoring on `tests` would drop the package.
    for anchor in ("packages", "tests"):
        if anchor in parts:
            return "/".join(parts[parts.index(anchor) :])
    return path.as_posix()


def tier_of_directory(rel: str) -> str:
    """The tier a module in this location must declare."""
    for prefix, tier in TIERS.items():
        if rel.startswith(prefix + "/"):
            return tier
    return "unit"  # packages/*/tests and anything else with no external deps


def declared_tiers(path: Path) -> list[str]:
    """Tier markers named by a module-level `pytestmark`, via AST."""
    tree = ast.parse(path.read_text(), str(path))
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", None) == "pytestmark" for t in node.targets):
            continue
        value = node.value
        items = value.elts if isinstance(value, ast.List | ast.Tuple) else [value]
        for item in items:
            if isinstance(item, ast.Attribute) and item.attr in ALL_TIERS:
                names.append(item.attr)
    return names


def per_function_tiers(path: Path) -> list[str]:
    """Tier markers applied as decorators, which the rule forbids."""
    tree = ast.parse(path.read_text(), str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Attribute) and dec.attr in ALL_TIERS:
                found.append(f"{_rel(path)}::{node.name} @pytest.mark.{dec.attr}")
    return found


def tier_offenders(paths=None) -> list[str]:
    """Modules whose declared tier is missing, doubled, or wrong for its home."""
    bad = []
    for path in paths if paths is not None else all_test_modules():
        rel = _rel(path)
        tiers = declared_tiers(path)
        want = tier_of_directory(rel)
        if len(tiers) != 1:
            bad.append(f"{rel}: declares {len(tiers)} tier markers, wants exactly 1 ({want})")
        elif tiers[0] != want:
            bad.append(f"{rel}: declares '{tiers[0]}', but its directory means '{want}'")
        bad += per_function_tiers(path)
    return bad


def repeated_tag_offenders(paths=None) -> list[str]:
    """Files whose name restates the directory holding them."""
    bad = []
    for path in paths if paths is not None else all_test_modules():
        rel = _rel(path)
        stem = path.stem
        for prefix, tags in DIRECTORY_TAGS.items():
            if not rel.startswith(prefix + "/"):
                continue
            for tag in tags:
                if tag in stem.split("_")[1:]:
                    bad.append(f"{rel}: name repeats '{tag}', which the directory already says")
    return bad


def qualifier_offenders(paths=None) -> list[str]:
    """Files placing a qualifier anywhere but the end of the name."""
    bad = []
    for path in paths if paths is not None else all_test_modules():
        rel = _rel(path)
        tokens = path.stem.split("_")[1:]
        for index, token in enumerate(tokens):
            if token not in QUALIFIERS:
                continue
            if any(later not in QUALIFIERS for later in tokens[index + 1 :]):
                bad.append(f"{rel}: '{token}' qualifies the subject, so it belongs at the end")
    return bad


def test_every_module_declares_one_tier_matching_its_directory():
    offenders = tier_offenders()
    assert not offenders, "tier markers that do not match their home:\n" + "\n".join(offenders)


def test_no_filename_repeats_its_directory():
    offenders = repeated_tag_offenders()
    assert not offenders, "names that restate their directory:\n" + "\n".join(offenders)


def test_every_qualifier_sits_at_the_end_of_the_name():
    offenders = qualifier_offenders()
    assert not offenders, "qualifiers out of position:\n" + "\n".join(offenders)


def test_the_sweep_reads_the_real_tree():
    """A rule that matched nothing would pass all three checks above."""
    modules = all_test_modules()
    assert len(modules) > 150, (
        f"only {len(modules)} modules found; the sweep is not reading the tree"
    )
    assert any(_rel(p).startswith("packages/") for p in modules), "packages/ not reached"
    assert any(_rel(p).startswith("tests/repo/") for p in modules), "tests/repo/ not reached"


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_CONTROL_a_module_with_no_tier_is_caught(tmp_path: Path):
    path = _write(tmp_path, "tests/unit/test_x.py", "import pytest\n\n\ndef test_a():\n    pass\n")
    assert tier_offenders([path])


def test_CONTROL_a_module_with_the_wrong_tier_is_caught(tmp_path: Path):
    path = _write(
        tmp_path,
        "tests/integration/test_x.py",
        "import pytest\n\npytestmark = pytest.mark.unit\n\n\ndef test_a():\n    pass\n",
    )
    assert tier_offenders([path])


def test_CONTROL_a_per_function_tier_decorator_is_caught(tmp_path: Path):
    path = _write(
        tmp_path,
        "tests/unit/test_x.py",
        "import pytest\n\npytestmark = pytest.mark.unit\n\n\n"
        "@pytest.mark.unit\ndef test_a():\n    pass\n",
    )
    assert tier_offenders([path])


def test_CONTROL_a_correct_module_is_not_caught(tmp_path: Path):
    path = _write(
        tmp_path,
        "tests/unit/test_x.py",
        "import pytest\n\npytestmark = pytest.mark.unit\n\n\ndef test_a():\n    pass\n",
    )
    assert not tier_offenders([path])


def test_CONTROL_a_repeated_directory_tag_is_caught(tmp_path: Path):
    path = _write(tmp_path, "tests/integration/test_service_integration.py", "")
    assert repeated_tag_offenders([path])


def test_CONTROL_a_subject_that_merely_contains_the_word_is_not_caught(tmp_path: Path):
    path = _write(tmp_path, "tests/unit/test_integration_helpers.py", "")
    assert not repeated_tag_offenders([path])


def test_CONTROL_a_leading_qualifier_is_caught(tmp_path: Path):
    path = _write(tmp_path, "tests/unit/test_adversarial_rpc.py", "")
    assert qualifier_offenders([path])


def test_CONTROL_a_trailing_qualifier_is_not_caught(tmp_path: Path):
    path = _write(tmp_path, "tests/unit/test_rpc_adversarial.py", "")
    assert not qualifier_offenders([path])


def test_CONTROL_stacked_trailing_qualifiers_are_not_caught(tmp_path: Path):
    """`_adversarial_stress` is two qualifiers in a row, not one out of place."""
    path = _write(tmp_path, "tests/unit/test_health_listener_adversarial_stress.py", "")
    assert not qualifier_offenders([path])

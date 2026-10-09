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
import os
import subprocess
import sys
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
            target = item.func if isinstance(item, ast.Call) else item
            if isinstance(target, ast.Attribute) and target.attr in ALL_TIERS:
                names.append(target.attr)
    return names


def per_function_tiers(path: Path) -> list[str]:
    """Tier markers applied as decorators, which the rule forbids."""
    tree = ast.parse(path.read_text(), str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr in ALL_TIERS:
                found.append(f"{_rel(path)}::{node.name} @pytest.mark.{target.attr}")
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


def test_CONTROL_a_per_function_tier_decorator_call_form_is_caught(tmp_path: Path):
    """Verify tier marker applied as a call decorator is detected and rejected."""
    path = _write(
        tmp_path,
        "tests/unit/test_x.py",
        "import pytest\n\npytestmark = pytest.mark.unit\n\n\n"
        "@pytest.mark.integration()\ndef test_a():\n    pass\n",
    )
    offenders = tier_offenders([path])
    assert offenders, "Call-form per-function tier marker was not detected"
    assert "@pytest.mark.integration" in offenders[0]


def test_CONTROL_declared_tier_call_form_is_recognized(tmp_path: Path):
    """Verify module-level pytestmark assigned via call form is parsed correctly."""
    path = _write(
        tmp_path,
        "tests/unit/test_call_mark.py",
        "import pytest\n\npytestmark = pytest.mark.unit()\n\n\ndef test_a():\n    pass\n",
    )
    assert declared_tiers(path) == ["unit"]
    assert not tier_offenders([path])


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


# The one place `.is_dir()` on a .git path is the point rather than the defect:
# the control that demonstrates a worktree pointer file is not a directory,
# which is the whole reason the guards ask `.exists()`. Exempted by function
# name, and asserted to still exist, so it cannot quietly cover anything else.
IS_DIR_EXEMPT = {
    (
        "tests/repo/test_the_suite_follows_its_conventions.py",
        "test_CONTROL_worktree_file_satisfies_exists",
    )
}


def _enclosing_function(tree: ast.AST, lineno: int) -> str | None:
    """Return the name of the top-level function containing `lineno`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                return node.name
    return None


def _is_dot_git_path(node: ast.AST | None) -> bool:
    """True for the expression `<anything> / ".git"`."""
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and node.right.value == ".git"
    )


def git_dir_check_offenders(paths=None) -> list[str]:
    """Test modules checking .git with .is_dir() rather than .exists().

    In Git worktrees, .git is a pointer file rather than a directory.
    Guards must check .exists() so they do not skip in worktrees.
    """
    bad = []
    for path in paths if paths is not None else all_test_modules():
        rel = _rel(path)
        if not rel.startswith("tests/repo/"):
            continue
        tree = ast.parse(path.read_text(), str(path))

        # Names bound to a `<expr> / ".git"` path anywhere in the module, so
        # the two-line spelling -- bind it, then ask -- is not a way out.
        git_paths: set[str] = set()
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            if not targets or not _is_dot_git_path(getattr(node, "value", None)):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    git_paths.add(target.id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "is_dir"):
                continue
            val = func.value
            names_git = _is_dot_git_path(val) or (isinstance(val, ast.Name) and val.id in git_paths)
            if names_git and (rel, _enclosing_function(tree, node.lineno)) not in IS_DIR_EXEMPT:
                bad.append(
                    f"{rel}:{node.lineno}: checks .git with .is_dir(); "
                    "use .exists() for worktree compatibility"
                )
    return bad


def test_no_repo_guard_checks_git_directory_shape():
    """Ensure repository guards do not check .is_dir() on .git."""
    offenders = git_dir_check_offenders()
    assert not offenders, "repository guards checking .git with .is_dir():\n" + "\n".join(offenders)


def test_git_repository_guards_do_not_skip_in_worktrees():
    """No repository guard skips for want of git in this checkout.

    Asserting `(REPO / ".git").exists()` would re-evaluate the very predicate
    the fixtures use, so it cannot fail for any reason other than a genuinely
    absent .git -- including the case it was written for, a worktree where the
    guards skip. This collects tests/repo in a subprocess and reads the skips
    back instead.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/repo", "-p", "no:cacheprovider", "-q", "-rs"],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTEST_ADDOPTS": "--collect-only"},
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]

    skipped = [
        line for line in (result.stdout + result.stderr).splitlines() if "release tarball" in line
    ]
    assert not skipped, (
        "repository guards are skipping for want of git in this checkout:\n  "
        + "\n  ".join(skipped)
    )


def test_the_is_dir_exemption_names_a_function_that_exists():
    """An exemption for a function that has moved stops exempting and starts hiding."""
    assert IS_DIR_EXEMPT, "the exemption set is empty; delete it and the branch that reads it"
    for rel, func in sorted(IS_DIR_EXEMPT):
        path = REPO / rel
        assert path.is_file(), f"exempted file does not exist: {rel}"
        tree = ast.parse(path.read_text(), str(path))
        names = {
            n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert func in names, f"{rel} no longer defines {func}; remove the exemption"


def test_no_repo_guard_defines_its_own_git_fixture():
    """The git fixture lives in tests/repo/conftest.py, once.

    A copy per module is how the check came to disagree with itself: a fix
    applied to one spelling left the others skipping. This is what stops a copy
    returning one file at a time.
    """
    conftest = REPO / "tests" / "repo" / "conftest.py"
    assert conftest.is_file(), "tests/repo/conftest.py is missing; the shared fixture lives there"
    assert "_require_git" in conftest.read_text(), (
        "tests/repo/conftest.py no longer defines _require_git"
    )

    offenders = []
    for path in all_test_modules():
        rel = _rel(path)
        if not rel.startswith("tests/repo/") or rel.endswith("/conftest.py"):
            continue
        tree = ast.parse(path.read_text(), str(path))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_require_git":
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "these define their own copy of the git fixture instead of taking the "
        "one in tests/repo/conftest.py:\n  " + "\n  ".join(offenders)
    )


def test_CONTROL_git_is_dir_check_is_caught(tmp_path: Path):
    path = _write(
        tmp_path,
        "tests/repo/test_sample.py",
        "import pytest\nfrom pathlib import Path\n"
        "REPO = Path('.')\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _require_git():\n"
        "    if not (REPO / '.git').is_dir():\n"
        "        pytest.skip('Not git')\n",
    )
    assert git_dir_check_offenders([path])


def test_CONTROL_git_exists_check_is_not_caught(tmp_path: Path):
    path = _write(
        tmp_path,
        "tests/repo/test_sample.py",
        "import pytest\nfrom pathlib import Path\n"
        "REPO = Path('.')\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _require_git():\n"
        "    if not (REPO / '.git').exists():\n"
        "        pytest.skip('Not git')\n",
    )
    assert not git_dir_check_offenders([path])


def test_CONTROL_worktree_file_satisfies_exists(tmp_path: Path):
    """Verify that a worktree pointer file satisfies .exists() and fails .is_dir()."""
    git_file = tmp_path / ".git"
    git_file.write_text("gitdir: /path/to/main/.git/worktrees/branch\n")
    assert git_file.exists()
    assert not git_file.is_dir()


def git_skip_rationale_offenders(paths=None) -> list[str]:
    """Functions skipping on .git absence without documented rationale.

    Any test or fixture checking .git presence and skipping must document
    its rationale (such as intentional skip for unpacked release tarball users).
    """
    bad = []
    for path in paths if paths is not None else all_test_modules():
        rel = _rel(path)
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            has_git_check = False
            has_skip = False
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.BinOp)
                    and isinstance(child.op, ast.Div)
                    and isinstance(child.right, ast.Constant)
                    and child.right.value == ".git"
                ):
                    has_git_check = True
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "skip"
                ):
                    has_skip = True
            if has_git_check and has_skip:
                docstring = ast.get_docstring(node) or ""
                doc_lower = docstring.lower()
                if not ("tarball" in doc_lower and "intentional" in doc_lower):
                    bad.append(
                        f"{rel}:{node.lineno}: {node.name} skips on .git without "
                        "documenting intentional release tarball rationale in its docstring"
                    )
    return bad


def test_git_skip_documents_tarball_rationale():
    """Ensure any guard that skips when .git is absent documents its rationale."""
    offenders = git_skip_rationale_offenders()
    assert not offenders, (
        "tests or fixtures skipping on .git absence without documented rationale:\n"
        + "\n".join(offenders)
    )


def test_CONTROL_git_skip_without_rationale_is_caught(tmp_path: Path):
    path = _write(
        tmp_path,
        "tests/repo/test_sample.py",
        "import pytest\nfrom pathlib import Path\n"
        "REPO = Path('.')\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def _require_git():\n"
        "    if not (REPO / '.git').exists():\n"
        "        pytest.skip('Not git')\n",
    )
    assert git_skip_rationale_offenders([path])


def test_CONTROL_git_skip_with_rationale_is_not_caught(tmp_path: Path):
    path = _write(
        tmp_path,
        "tests/repo/test_sample.py",
        "import pytest\nfrom pathlib import Path\n"
        'REPO = Path(".")\n\n'
        "@pytest.fixture(autouse=True)\n"
        "def _require_git():\n"
        '    """Intentional design for release tarball environments."""\n'
        '    if not (REPO / ".git").exists():\n'
        '        pytest.skip("Not git")\n',
    )
    assert not git_skip_rationale_offenders([path])

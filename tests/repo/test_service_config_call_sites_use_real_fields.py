"""Verify every `ServiceConfig(...)` call in the repo passes only real fields.

Complements test_service_config_unknown_fields.py by statically
checking call sites across all tracked repository files to ensure no
obsolete arguments are passed.
"""

import ast
import subprocess
from pathlib import Path

import pytest

from cliffracer import ServiceConfig

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# The deliberate non-fields in the tree: the negative cases proving a bad field is refused by the
# model. Three of them pass the field through a `**{...}` literal, which the sweep reads as it
# reads a keyword. Exempted BY NAME so another one is a failure rather than a silently widened
# rule.
EXEMPT = {
    ("tests/unit/test_service_config_unknown_fields.py", "nats_ul"),
    ("tests/unit/test_msgpack_serialization.py", "invalid_field"),
    ("tests/unit/test_service_config_bounds_adversarial.py", "unregistered_field"),
    ("tests/unit/test_a_service_config_does_not_print_its_credentials.py", "nats_passwrd"),
    (
        "tests/unit/test_the_framework_argument_checks_raise_the_exported_validation_error.py",
        "a_key_it_does_not_have",
    ),
}

REQUIRED_TREES = (
    "src",
    "packages",
    "tests",
    "examples",
    "example_consumer",
    "load-testing",
)


def _tracked_python_files(root: Path = REPO) -> list[Path]:
    """Return all tracked Python files in the repository."""
    if (root / ".git").exists():
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "*.py"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        return [root / rel for rel in out]
    return [
        p
        for p in root.rglob("*.py")
        if not any(part.startswith(".") for part in p.relative_to(root).parts)
        and "__pycache__" not in p.parts
    ]


def _service_config_names(tree: ast.AST) -> set[str]:
    """The names a module can call `ServiceConfig` by.

    The class's own name, every alias an import gives it (`from cliffracer import ServiceConfig
    as SC`), and a name assigned from either (`SC = ServiceConfig`). A call spelled as an
    attribute (`cliffracer.ServiceConfig(...)`, `core.service_config.ServiceConfig(...)`) is
    matched on the attribute itself, in `_is_a_service_config_call`.
    """
    names = {"ServiceConfig"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "ServiceConfig" and (alias.asname or alias.name) not in names:
                        names.add(alias.asname or alias.name)
                        changed = True
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
                if (
                    isinstance(target, ast.Name)
                    and target.id not in names
                    and (
                        (isinstance(value, ast.Name) and value.id in names)
                        or (isinstance(value, ast.Attribute) and value.attr == "ServiceConfig")
                    )
                ):
                    names.add(target.id)
                    changed = True
    return names


def _is_a_service_config_call(node: ast.AST, names: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id in names) or (
        isinstance(func, ast.Attribute) and func.attr == "ServiceConfig"
    )


def _keywords_of(call: ast.Call) -> list[str]:
    """Every field name a call passes: its keywords and the string keys of a dict-literal spread.

    `**some_mapping` whose source is not a literal cannot be read statically, and is left to the
    model itself: `ServiceConfig` forbids an unknown field, so a bad key in one raises where it
    is used. A literal spread has its keys in plain sight, so it is checked like a keyword.
    """
    found = []
    for kw in call.keywords:
        if kw.arg is not None:
            found.append(kw.arg)
        elif isinstance(kw.value, ast.Dict):
            found += [
                key.value
                for key in kw.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
    return found


def _call_sites(root: Path = REPO):
    """(relative path, lineno, kwarg) for every field named in every ServiceConfig() call.

    A call is found by any name the module gives the class (an alias, an assigned alias) or by
    the attribute spelling, and a field is a keyword or a string key of a dict-literal `**` spread.
    A spread of anything else is the one thing this cannot see; `extra="forbid"` covers it at run
    time.
    """
    for path in _tracked_python_files(root):
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as err:
            raise SyntaxError(
                f"Syntax error parsing {path.relative_to(root)}:{err.lineno}: {err.msg}"
            ) from err
        names = _service_config_names(tree)
        for node in ast.walk(tree):
            if _is_a_service_config_call(node, names):
                assert isinstance(node, ast.Call)
                for arg in _keywords_of(node):
                    yield path.relative_to(root).as_posix(), node.lineno, arg


def test_no_call_site_passes_a_field_that_does_not_exist():
    fields = set(ServiceConfig.model_fields)
    bad = [
        f"{rel}:{line}: ServiceConfig({arg}=...) is not a ServiceConfig field"
        for rel, line, arg in _call_sites()
        if arg not in fields and (rel, arg) not in EXEMPT
    ]
    assert not bad, "Found call sites passing invalid fields to ServiceConfig:\n" + "\n".join(bad)


def test_CONTROL_the_sweep_finds_call_sites_at_all():
    """Verify the sweep reads every tracked Python file and reaches every tree.

    Discovery is compared against the index rather than against a chosen
    number. A floor like "at least 1000 keywords" is dominated by one large
    tree, so it stays satisfied while another stops being read entirely.
    """
    sites = list(_call_sites())

    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    tracked = {REPO / rel for rel in listed}
    assert tracked, "git lists no Python files; this control is stale"

    unread = tracked - set(_tracked_python_files())
    assert not unread, (
        f"{len(unread)} tracked Python file(s) are not read by the sweep, for "
        f"example {sorted(str(q.relative_to(REPO)) for q in unread)[:3]}"
    )

    for tree in REQUIRED_TREES:
        assert any(rel.startswith(f"{tree}/") for rel, _, _ in sites), (
            f"sweep failed to find any ServiceConfig calls in '{tree}/'"
        )


def test_CONTROL_the_sweep_reaches_a_call_site_at_the_repository_root(tmp_path: Path):
    """A root-level file is read as far as its call sites, not only listed.

    The live tree has no ServiceConfig call at the root, so this plants one
    beside a nested one and asserts both are reported.
    """
    (tmp_path / "tool.py").write_text('ServiceConfig(name="root_tool")\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "svc.py").write_text('ServiceConfig(name="nested")\n')

    sites = {(rel, arg) for rel, _, arg in _call_sites(tmp_path)}

    assert ("tool.py", "name") in sites, sites
    assert ("src/svc.py", "name") in sites, sites


def test_CONTROL_the_exemption_still_describes_something_real():
    """An exemption for a call site that has moved silently stops exempting and
    starts hiding nothing -- but an exemption nobody notices is stale is worse,
    so assert the one we carry is still there."""
    found = {(rel, arg) for rel, _, arg in _call_sites()}
    assert EXEMPT <= found, f"exemption no longer matches any call site: {EXEMPT - found}"


def _fields_found_in(tmp_path: Path, source: str) -> list[str]:
    """What the real sweep reports for one planted module."""
    (tmp_path / "planted.py").write_text(source)
    return [arg for _, _, arg in _call_sites(tmp_path)]


PLANTED = {
    "an-aliased-import": "from cliffracer import ServiceConfig as SC\nSC(name='x', bogus=1)\n",
    "an-alias-from-the-defining-module": (
        "from cliffracer.core.service_config import ServiceConfig as Cfg\nCfg(bogus=1)\n"
    ),
    "an-assigned-alias": "from cliffracer import ServiceConfig\nSC = ServiceConfig\nSC(bogus=1)\n",
    "a-chain-of-assigned-aliases": "A = ServiceConfig\nB = A\nB(bogus=1)\n",
    "a-chain-written-in-the-other-order": "B = A\nA = ServiceConfig\nB(bogus=1)\n",
    "the-attribute-spelling": "import cliffracer\ncliffracer.ServiceConfig(bogus=1)\n",
    "a-nested-attribute-spelling": (
        "import cliffracer as c\nc.core.service_config.ServiceConfig(bogus=1)\n"
    ),
    "a-dict-literal-spread": "ServiceConfig(**{'bogus': 1})\n",
    "a-dict-literal-spread-beside-keywords": "ServiceConfig(name='x', **{'bogus': 1})\n",
}


@pytest.mark.parametrize("source", PLANTED.values(), ids=PLANTED.keys())
def test_CONTROL_the_sweep_finds_a_bad_field_however_the_class_is_named_or_passed(
    tmp_path: Path, source: str
):
    assert "bogus" in _fields_found_in(tmp_path, source)


def test_CONTROL_the_sweep_does_not_claim_an_unrelated_call(tmp_path: Path):
    source = "class Other:\n    pass\nOther(bogus=1)\nx = Other\nx(bogus=2)\n"

    assert _fields_found_in(tmp_path, source) == []


def test_the_one_spread_the_sweep_cannot_read_is_named_here(tmp_path: Path):
    """A `**` of anything that is not a literal is left to the model, which forbids an unknown
    field. This pins that it is unread, so a change that starts to read it is a decision."""
    assert _fields_found_in(tmp_path, "ServiceConfig(name='x', **options)\n") == ["name"]

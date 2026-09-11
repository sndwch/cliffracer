"""Verify every `ServiceConfig(...)` call in the repo passes only real fields.

Complements test_service_config_rejects_unknown_fields.py by statically
checking call sites across src, packages, tests, and examples to ensure no
obsolete arguments are passed.
"""

import ast
from pathlib import Path

import pytest

from cliffracer import ServiceConfig

REPO = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "packages", "tests", "examples")

# The one deliberate non-field in the tree: the negative case proving a
# misspelling is refused by name. Exempted BY NAME so a second one is a
# failure rather than a silently widened rule.
EXEMPT = {("tests/unit/test_service_config_rejects_unknown_fields.py", "nats_ul")}


def _call_sites():
    """(relative path, lineno, kwarg) for every keyword of every ServiceConfig()."""
    for d in SCAN_DIRS:
        base = REPO / d
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "ServiceConfig"
                ):
                    for kw in node.keywords:
                        if kw.arg is not None:
                            yield path.relative_to(REPO).as_posix(), node.lineno, kw.arg


@pytest.mark.unit
def test_no_call_site_passes_a_field_that_does_not_exist():
    fields = set(ServiceConfig.model_fields)
    bad = [
        f"{rel}:{line}: ServiceConfig({arg}=...) is not a ServiceConfig field"
        for rel, line, arg in _call_sites()
        if arg not in fields and (rel, arg) not in EXEMPT
    ]
    assert not bad, "\n".join(bad)


@pytest.mark.unit
def test_CONTROL_the_sweep_finds_call_sites_at_all():
    """Without this, a sweep that walks nothing passes the assertion above --
    which is exactly how a deletion that breaks call sites reads as green."""
    sites = list(_call_sites())
    assert len(sites) > 50, f"only {len(sites)} keywords found; has the sweep stopped walking?"
    assert any(rel.startswith("examples/") for rel, _, _ in sites), "examples/ not reached"
    assert any(rel.startswith("tests/") for rel, _, _ in sites), "tests/ not reached"


@pytest.mark.unit
def test_CONTROL_the_exemption_still_describes_something_real():
    """An exemption for a call site that has moved silently stops exempting and
    starts hiding nothing -- but an exemption nobody notices is stale is worse,
    so assert the one we carry is still there."""
    found = {(rel, arg) for rel, _, arg in _call_sites()}
    assert EXEMPT <= found, f"exemption no longer matches any call site: {EXEMPT - found}"

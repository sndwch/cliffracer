"""Read generated clients as data and explain differences without importing them."""

from __future__ import annotations

import ast
from pathlib import Path


def _rpcs(source: bytes) -> dict[str, tuple[str | None, str | None]] | None:
    """Read signature hashes and method definitions from the generated layout."""
    try:
        tree = ast.parse(source)
        assignments = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "SIGNATURES" for target in targets
            ):
                assignments.append(node.value)
        if len(assignments) != 1 or assignments[0] is None:
            return None
        signatures = ast.literal_eval(assignments[0])
    except (SyntaxError, ValueError, TypeError, RecursionError):
        return None
    if not isinstance(signatures, dict) or not all(
        isinstance(name, str) and isinstance(value, str) for name, value in signatures.items()
    ):
        return None
    clients = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "ServiceClient" for base in node.bases)
    ]
    if len(clients) != 1:
        return None
    methods = {
        node.name: ast.dump(node, include_attributes=False)
        for node in clients[0].body
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
    }
    return {
        name: (signatures.get(name), methods.get(name))
        for name in signatures.keys() | methods.keys()
    }


def check_client(target: Path, source: str) -> list[str]:
    """Return drift diagnostics, or an empty list for an exact byte match.

    Missing files are drift. Other read errors propagate so the command can
    distinguish an inaccessible output from a client that needs regeneration.
    The signature table and method bodies only explain drift; they never replace
    the comparison of the complete generated file.
    """
    expected_bytes = source.encode("utf-8")
    expected = _rpcs(expected_bytes)
    try:
        existing_bytes = target.read_bytes()
    except FileNotFoundError:
        missing_names = ", ".join(sorted(expected or {}))
        return [f"missing generated client: {target}", f"missing RPCs: {missing_names}"]
    if existing_bytes == expected_bytes:
        return []

    differences = [f"stale generated client: {target}"]
    existing = _rpcs(existing_bytes)
    if existing is None or expected is None:
        differences.append("RPC differences unavailable: cannot read generated-client metadata.")
    else:
        missing = {name for name in expected if name not in existing or existing[name][1] is None}
        extra = existing.keys() - expected.keys()
        changed = {
            name
            for name in (existing.keys() & expected.keys()) - missing
            if existing[name] != expected[name]
        }
        for label, names in (("missing", missing), ("extra", extra), ("changed", changed)):
            if names:
                differences.append(f"{label} RPCs: " + ", ".join(sorted(names)))
        if not (missing or extra or changed):
            differences.append("RPC signatures and methods match; other generated content differs.")
    return differences

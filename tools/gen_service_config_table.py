#!/usr/bin/env python3
"""Generator for the ServiceConfig documentation table in docs/api-reference.md.

    python3 tools/gen_service_config_table.py          # rewrite the block
    python3 tools/gen_service_config_table.py --check  # exit 1 if stale

The table is GENERATED, never retyped: it has one row per
``ServiceConfig.model_fields`` entry, so a field added, removed or given a new
default cannot drift from the doc without the guard noticing.

A field's description comes from ``Field(description=...)`` when it has one, and
that text always wins: a row edited by hand to say something else is rewritten,
and ``--check`` reports it stale. Only a field with no description in the model
keeps the sentence a human wrote in its row, which is the one cell nothing
else derives.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "api-reference.md"
START = "<!-- service-config-fields -->"
END = "<!-- /service-config-fields -->"


def table_block(text: str) -> tuple[str, str, str] | None:
    """The document split around the table: what precedes the start marker, the table between
    the markers, and what follows the end marker. None when either marker is missing.

    The generator rewrites only this block, and `tests/repo/test_every_config_field_is_described.py`
    reads its rows only from it, so both agree on what "the table" is: another table in the same
    document with a backticked first column is neither rewritten nor read as config fields.
    """
    if START not in text or END not in text:
        return None
    head, rest = text.split(START, 1)
    block, tail = rest.split(END, 1)
    return head, block, tail


def _default_repr(field) -> str:
    from pydantic_core import PydanticUndefined

    if field.default is not PydanticUndefined:
        return repr(field.default)
    if field.default_factory is not None:
        # A default that reads the environment (`subject_prefix` reads
        # `$CLIFFRACER_SUBJECT_PREFIX`) is documented as the default the code has, not as the
        # value of whatever shell ran the generator.
        saved = {k: v for k, v in os.environ.items() if k.startswith("CLIFFRACER_")}
        for key in saved:
            del os.environ[key]
        try:
            return repr(field.default_factory())
        finally:
            os.environ.update(saved)
    return "required"


def render(existing: str = "") -> str:
    sys.path.insert(0, str(REPO / "src"))
    from cliffracer import ServiceConfig

    # A field with no description in the model keeps the one a human wrote in its row.
    kept = dict(re.findall(r"^\| `(\w+)` \| `[^`]*` \| ([^|]*)\|", existing, flags=re.M))

    rows = [
        "| field | default | description |",
        "|---|---|---|",
    ]
    for name, field in ServiceConfig.model_fields.items():
        desc = (field.description or "").strip() or kept.get(name, "").strip()
        assert "|" not in desc, f"{name}: a description with a pipe would split its table row"
        rows.append(f"| `{name}` | `{_default_repr(field)}` | {desc} |")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument(
        "--doc",
        type=Path,
        default=DOC,
        help="the document to read or rewrite. Exists so the checker's own "
        "control can mutate a COPY: a control that edits the tracked file and "
        "restores it in a finally leaves a corrupted doc if the run is killed, "
        "and the next --check then fails saying the table is stale, which "
        "points the reader at the wrong thing.",
    )
    args = ap.parse_args()

    doc = args.doc
    text = doc.read_text()
    split = table_block(text)
    if split is None:
        print(f"{doc}: missing {START} / {END} markers", file=sys.stderr)
        return 2

    head, old, tail = split
    new = f"\n{render(old)}\n"

    if args.check:
        if old != new:
            print(
                "docs/api-reference.md ServiceConfig table is stale; run "
                "python3 tools/gen_service_config_table.py",
                file=sys.stderr,
            )
            return 1
        return 0

    doc.write_text(head + START + new + END + tail)
    print(f"regenerated {len(new.strip().splitlines()) - 2} rows in {doc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

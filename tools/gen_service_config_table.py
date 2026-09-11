#!/usr/bin/env python3
"""Generator for the ServiceConfig documentation table in docs/api-reference.md.

    python3 tools/gen_service_config_table.py          # rewrite the block
    python3 tools/gen_service_config_table.py --check  # exit 1 if stale

The table is GENERATED, never retyped: it has one row per
``ServiceConfig.model_fields`` entry, so a field added, removed or given a new
default cannot drift from the doc without the guard noticing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "api-reference.md"
START = "<!-- service-config-fields -->"
END = "<!-- /service-config-fields -->"


def _default_repr(field) -> str:
    from pydantic_core import PydanticUndefined

    if field.default is not PydanticUndefined:
        return repr(field.default)
    if field.default_factory is not None:
        return repr(field.default_factory())
    return "required"


def render(existing: str = "") -> str:
    sys.path.insert(0, str(REPO / "src"))
    from cliffracer import ServiceConfig

    # Keep any description a human has written for a field that still exists.
    kept = dict(re.findall(r"^\| `(\w+)` \| `[^`]*` \| ([^|]*)\|", existing, flags=re.M))

    rows = [
        "| field | default | description |",
        "|---|---|---|",
    ]
    for name, field in ServiceConfig.model_fields.items():
        desc = kept.get(name, "").strip() or (field.description or "")
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
    if START not in text or END not in text:
        print(f"{doc}: missing {START} / {END} markers", file=sys.stderr)
        return 2

    head, rest = text.split(START, 1)
    old, tail = rest.split(END, 1)
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

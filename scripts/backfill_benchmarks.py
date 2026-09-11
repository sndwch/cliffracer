#!/usr/bin/env python3
"""Traverse git tags and main to backfill benchmark history.

Gracefully skips extension packages not yet introduced at earlier tags.
Generates benchmarks_history.json and updates docs/benchmarks.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from run_benchmarks import generate_benchmarks_markdown  # noqa: E402

from tests.benchmark.benchmarks import (  # noqa: E402
    DEFAULT_NATS_URL,
    benchmark_auth,
    benchmark_faststream,
    benchmark_http_gateway,
    benchmark_jetstream,
    benchmark_kv,
    benchmark_rpc,
    benchmark_serialization,
)


def get_target_tags() -> list[str]:
    """Return ordered list of tags to backfill."""
    try:
        res = subprocess.run(
            ["git", "tag", "-l"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        all_tags = [t.strip() for t in res.stdout.splitlines() if t.strip()]
    except Exception:
        all_tags = []

    # Filter tags in the v1.x range
    selected: list[str] = [t for t in all_tags if t.startswith("v1.")]

    # Always include current main/HEAD
    selected.append("main")
    return selected


async def run_benchmarks_for_worktree(
    worktree_path: Path,
    tag: str,
    nats_url: str = DEFAULT_NATS_URL,
) -> dict[str, Any]:
    """Run applicable benchmarks for the checked-out tag, skipping unintroduced packages."""
    print(f"\n---> Benchmarking {tag} at {worktree_path}...")
    metrics: dict[str, Any] = {}

    # 1. Core RPC
    print("     [1/7] Core RPC benchmarks...")
    try:
        metrics["rpc"] = await benchmark_rpc(nats_url)
    except Exception as exc:
        metrics["rpc"] = {"skipped": str(exc)}

    # 2. Core JetStream
    print("     [2/7] Core JetStream pull benchmarks...")
    try:
        metrics["jetstream"] = await benchmark_jetstream(nats_url)
    except Exception as exc:
        metrics["jetstream"] = {"skipped": str(exc)}

    # 3. Serialization
    print("     [3/7] Serialization benchmarks...")
    try:
        metrics["serialization"] = benchmark_serialization()
    except Exception as exc:
        metrics["serialization"] = {"skipped": str(exc)}

    # Check extension presence in this tag
    packages_dir = worktree_path / "packages"
    has_http = (packages_dir / "cliffracer-http").is_dir()
    has_faststream = (packages_dir / "cliffracer-faststream").is_dir()
    has_auth = (packages_dir / "cliffracer-auth").is_dir()
    has_kv = (packages_dir / "cliffracer-kv").is_dir()

    # 4. HTTP Gateway
    if has_http:
        print("     [4/7] cliffracer-http benchmarks...")
        try:
            metrics["http_gateway"] = await benchmark_http_gateway()
        except Exception as exc:
            metrics["http_gateway"] = {"skipped": str(exc)}
    else:
        print("     [4/7] cliffracer-http: Skipped (package not introduced in this version)")
        metrics["http_gateway"] = {"skipped": "Package not introduced in this version"}

    # 5. FastStream
    if has_faststream:
        print("     [5/7] cliffracer-faststream benchmarks...")
        try:
            metrics["faststream"] = await benchmark_faststream()
        except Exception as exc:
            metrics["faststream"] = {"skipped": str(exc)}
    else:
        print("     [5/7] cliffracer-faststream: Skipped (package not introduced in this version)")
        metrics["faststream"] = {"skipped": "Package not introduced in this version"}

    # 6. Auth
    if has_auth:
        print("     [6/7] cliffracer-auth benchmarks...")
        try:
            metrics["auth"] = benchmark_auth()
        except Exception as exc:
            metrics["auth"] = {"skipped": str(exc)}
    else:
        print("     [6/7] cliffracer-auth: Skipped (package not introduced in this version)")
        metrics["auth"] = {"skipped": "Package not introduced in this version"}

    # 7. KV
    if has_kv:
        print("     [7/7] cliffracer-kv benchmarks...")
        try:
            metrics["kv"] = await benchmark_kv(nats_url)
        except Exception as exc:
            metrics["kv"] = {"skipped": str(exc)}
    else:
        print("     [7/7] cliffracer-kv: Skipped (package not introduced in this version)")
        metrics["kv"] = {"skipped": "Package not introduced in this version"}

    return {
        "tag": tag,
        "metrics": metrics,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill historical benchmarks across git tags")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "benchmarks_history.json",
        help="Path to output historical benchmarks JSON",
    )
    parser.add_argument(
        "--nats-url",
        type=str,
        default=DEFAULT_NATS_URL,
        help="NATS broker connection URL",
    )
    args = parser.parse_args()

    tags = get_target_tags()
    print(f"Starting Historical Backfill across tags: {', '.join(tags)}")

    history: dict[str, Any] = {}
    temp_dir_base = tempfile.mkdtemp(prefix="cliffracer-backfill-")

    try:
        for tag in tags:
            tag_slug = tag.replace("/", "-")
            wt_path = Path(temp_dir_base) / tag_slug
            try:
                # Add git worktree
                subprocess.run(
                    ["git", "worktree", "add", "--detach", str(wt_path), tag],
                    cwd=REPO_ROOT,
                    check=True,
                    capture_output=True,
                    timeout=15,
                )

                # Execute benchmarks for worktree
                tag_results = await run_benchmarks_for_worktree(
                    wt_path, tag, nats_url=args.nats_url
                )
                history[tag] = tag_results
            except Exception as exc:
                print(f"Error benchmarking {tag}: {exc}", file=sys.stderr)
            finally:
                if wt_path.exists():
                    try:
                        subprocess.run(
                            ["git", "worktree", "remove", "--force", str(wt_path)],
                            cwd=REPO_ROOT,
                            check=False,
                            capture_output=True,
                            timeout=10,
                        )
                    except Exception:
                        pass

        # Write benchmarks_history.json
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        print(f"\nSuccessfully wrote historical benchmarks to {args.output}")

        # Update docs/benchmarks.md with historical table
        baseline_path = REPO_ROOT / "benchmark_baseline.json"
        if baseline_path.is_file():
            baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))
        else:
            baseline_data = history.get("main", {})

        docs_dir = REPO_ROOT / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        benchmarks_md_path = docs_dir / "benchmarks.md"
        md_content = generate_benchmarks_markdown(baseline_data, args.output)
        benchmarks_md_path.write_text(md_content, encoding="utf-8")
        print(f"Successfully updated {benchmarks_md_path} with historical evolution table!")

    finally:
        shutil.rmtree(temp_dir_base, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

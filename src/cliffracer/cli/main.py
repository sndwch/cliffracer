"""`cliffracer run` command-line entrypoint."""

import argparse
import sys
from typing import Any

from loguru import logger

from cliffracer.runners.orchestrator import ServiceOrchestrator

from .config import ConfigError, build_overrides, load_yaml_config
from .discovery import DiscoveryError, resolve_targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cliffracer", description="Run Cliffracer services.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Discover and run one or more services.")
    run.add_argument(
        "targets",
        nargs="+",
        metavar="MODULE[:CLASS]",
        help="Service targets: 'pkg.mod:ServiceClass' or bare 'pkg.mod' to run all services in it.",
    )
    run.add_argument("--nats-url", default=None, help="Override NATS URL for all services.")
    run.add_argument("--log-level", default=None, help="Override log level for all services.")
    run.add_argument(
        "--config", dest="config_path", default=None, help="Path to a YAML config file."
    )
    return parser


def _flag_overrides(nats_url: str | None, log_level: str | None) -> dict[str, Any]:
    """Build flag override dict, including only non-None values."""
    overrides: dict[str, Any] = {}
    if nats_url is not None:
        overrides["nats_url"] = nats_url
    if log_level is not None:
        overrides["log_level"] = log_level
    return overrides


def flag_overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return _flag_overrides(
        nats_url=getattr(args, "nats_url", None),
        log_level=getattr(args, "log_level", None),
    )


def build_orchestrator(
    targets: list[str],
    *,
    nats_url: str | None,
    log_level: str | None,
    config_path: str | None,
) -> ServiceOrchestrator:
    """Resolve targets and assemble a configured orchestrator (no NATS I/O)."""
    classes = resolve_targets(targets)
    yaml_config = load_yaml_config(config_path)
    flag_overrides = _flag_overrides(nats_url, log_level)

    orchestrator = ServiceOrchestrator()
    for cls in classes:
        # Service name is known only after construction; use a throwaway instance
        # to read it for per-service YAML lookup.
        service_name = cls().config.name
        overrides = build_overrides(service_name, yaml_config, flag_overrides)
        orchestrator.add_service(cls, overrides=overrides)
    return orchestrator


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        # Ensure project-local modules resolve when invoked from the project root.
        if "" not in sys.path:
            sys.path.insert(0, "")

        try:
            orchestrator = build_orchestrator(
                args.targets,
                nats_url=args.nats_url,
                log_level=args.log_level,
                config_path=args.config_path,
            )
        except (DiscoveryError, ConfigError) as e:
            logger.error(str(e))
            return 2
        logger.info(f"Running {len(orchestrator.runners)} service(s)")
        orchestrator.run_forever()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

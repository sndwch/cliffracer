"""`cliffracer` command-line entrypoint: `run`, `describe` and `call`."""

import argparse
import os
import sys
from typing import Any

from loguru import logger

from cliffracer.core.construction import apply_config_overlay, overlay_refusal_text
from cliffracer.runners.orchestrator import ServiceOrchestrator

from .config import ConfigError, build_overrides, load_yaml_config
from .discovery import DiscoveryError, resolve_targets
from .operate import add_commands
from .operate import run as run_operation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cliffracer",
        description="Run Cliffracer services, and describe or call one that is running.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Discover and run one or more services.")
    run.add_argument(
        "targets",
        nargs="+",
        metavar="MODULE[:CLASS]",
        help="Service targets: 'pkg.mod:ServiceClass' or bare 'pkg.mod' to run all services in it.",
    )
    run.add_argument("--nats-url", default=None, help="Override NATS URL for all services.")
    run.add_argument(
        "--log-level",
        default=None,
        help="Log level for this process. Sinks are process-wide, so it applies "
        "to every service run here.",
    )
    run.add_argument(
        "--config", dest="config_path", default=None, help="Path to a YAML config file."
    )
    add_commands(sub)
    return parser


def _flag_overrides(nats_url: str | None) -> dict[str, Any]:
    """Build flag override dict, including only non-None values."""
    overrides: dict[str, Any] = {}
    if nats_url is not None:
        overrides["nats_url"] = nats_url
    return overrides


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
    flag_overrides = _flag_overrides(nats_url)

    orchestrator = ServiceOrchestrator(log_level=log_level)
    names: list[str] = []
    for cls in classes:
        # Service name is known only after construction; use a throwaway instance
        # to read it for per-service YAML lookup. A constructor that raises is a
        # target that cannot run, reported the way an unimportable module is, with
        # the exception kept as the cause so its traceback is logged.
        try:
            service = cls()
        except Exception as e:
            raise DiscoveryError(
                f"could not construct service {cls.__module__}:{cls.__qualname__}: "
                f"{type(e).__name__}: {e}"
            ) from e
        service_name = service.config.name
        names.append(service_name)
        overrides = build_overrides(service_name, yaml_config, flag_overrides)
        if overrides:
            # The overlay is validated as one config against the instance read above, so a wrong
            # value or an inconsistent pair is refused here, as an unknown key is, and not
            # retried by the runner. Not chained: the refusal's frames hold the overlay.
            try:
                apply_config_overlay(service, overrides)
            except ValueError as refusal:
                raise ConfigError(
                    f"the --config and flag settings for service {service_name!r} are refused: "
                    f"{overlay_refusal_text(refusal)}"
                ) from None
        orchestrator.add_service(cls, overrides=overrides)
    # A section for a name nobody in this run has is not an error (one file can serve several
    # deployments), but it is the shape a misspelt service name takes, so it is said.
    unused = sorted(set(yaml_config["services"]) - set(names))
    if unused:
        logger.warning(
            f"--config has settings for service(s) this command does not run: "
            f"{', '.join(unused)}. It runs: {', '.join(sorted(names))}"
        )
    return orchestrator


def _stop_printing_frame_values() -> None:
    """Replace loguru's default sink with one that prints a traceback's frames and not their values.

    The default sink has `diagnose=True`, which prints the value of every expression on each line
    of a traceback: a credential held by a failing frame, or by the config overlay laid over a
    service, would reach stderr. The replacement writes to stderr at the level the default sink
    had (`LOGURU_LEVEL`, else DEBUG). A host that already replaced the default sink keeps its own.
    """
    try:
        logger.remove(0)
    except ValueError:
        return
    logger.add(sys.stderr, level=os.environ.get("LOGURU_LEVEL") or "DEBUG", diagnose=False)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in ("describe", "call"):
        return run_operation(args)
    if args.command == "run":
        _stop_printing_frame_values()
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
            # With the cause's traceback when there is one: the message names
            # the exception, but a service module that raises at import is
            # debugged from its stack, and a one-line log hides it.
            logger.opt(exception=e.__cause__).error(str(e))
            return 2
        logger.info(f"Running {len(orchestrator.runners)} service(s)")
        orchestrator.run_forever()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

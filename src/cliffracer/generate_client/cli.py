"""cliffracer-generate-client: generate typed clients from service descriptions.

THE EXIT CODES ARE THE INTERFACE. A person reads the message; anything
scripting the generator reads the code:

    0  a client was written
    2  the broker answered but no such service did
    3  no broker at that address
    4  the service cannot be described, or its description cannot be emitted
    5  the class named by --class could not be imported

2 and 3 are deliberately different. "Nothing is listening" and "something is
listening and your service name is wrong" send a reader to different places,
and a single "connection problem" code sends them to the wrong one half the
time.

Every message names the thing the reader has to change -- the module, the
address, the model -- and every failure path leaves no file behind. A
half-written client is worse than none, because it imports.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from nats.errors import NoRespondersError, NoServersError
from nats.errors import TimeoutError as NatsTimeoutError

from cliffracer.core.typed_rpc import UntypedHandler, unimportable_models
from cliffracer.introspect import Description, describe

from .emitter import CannotEmit, emit

DEFAULT_URL = "nats://localhost:4222"


def resolve_nats_url(flag: str | None, environ: dict[str, str]) -> str:
    """Flag, then `$CLIFFRACER_NATS_URL`, then the default. One rule, testable."""
    return flag or environ.get("CLIFFRACER_NATS_URL") or DEFAULT_URL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cliffracer-generate-client",
        description="Generate a typed client for a cliffracer service.",
    )
    parser.add_argument("--service", required=True, help="service name (the subject prefix)")
    parser.add_argument("--namespace", default=None)
    parser.add_argument(
        "--nats-url",
        default=None,
        help="broker URL; falls back to CLIFFRACER_NATS_URL, then " + DEFAULT_URL,
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--class",
        dest="target",
        default=None,
        metavar="MODULE:CLASS",
        help="describe the class in-process instead of asking a live service",
    )
    parser.add_argument("--version", default="0", help="version to record in --class mode")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "header to send with the describe request, repeatable; a service behind "
            'AuthExtension needs --header authorization="bearer <token>"'
        ),
    )
    parser.add_argument("--out", default=None, help="write here instead of stdout")
    return parser


def parse_headers(pairs: list[str]) -> dict[str, str]:
    """`NAME=VALUE` strings into a header dict. The value may contain `=`."""
    headers: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"--header wants NAME=VALUE, got {pair!r}")
        headers[name.strip()] = value
    return headers


async def fetch_description(
    nats_url: str,
    service: str,
    namespace: str | None,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> dict:
    import nats

    # max_reconnect_attempts=1 bounds connection time when probing service description.
    nc = await nats.connect(
        nats_url,
        connect_timeout=timeout,
        max_reconnect_attempts=1,
        reconnect_time_wait=min(timeout, 0.5),
    )
    try:
        subject = f"{namespace}.{service}.describe" if namespace else f"{service}.describe"
        # Read raw response dict to handle error envelopes before parsing Description.
        reply = await nc.request(subject, b"", timeout=timeout, headers=headers)
        return cast(dict[Any, Any], json.loads(reply.data.decode()))
    finally:
        await nc.drain()


def describe_class(target: str, service: str, version: str) -> Description:
    module_name, _, cls_name = target.partition(":")
    module = importlib.import_module(module_name)
    cls = getattr(module, cls_name)
    return describe(cls, service=service, version=version)


def _unimportable(desc: Description) -> list[str]:
    offenders: set[str] = set()
    for method in desc.methods:
        for param in method.params:
            offenders.update(unimportable_models(param.type))
        offenders.update(unimportable_models(method.returns))
    return sorted(offenders)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    err = sys.stderr

    if args.target:
        try:
            desc = describe_class(args.target, args.service, args.version)
        except (ImportError, AttributeError, ValueError) as exc:
            print(
                f"could not import {args.target}: {exc}. Give MODULE:CLASS on the PYTHONPATH.",
                file=err,
            )
            return 5
        except UntypedHandler as exc:
            print(f"cannot describe {args.target}: {exc}", file=err)
            return 4
    else:
        url = resolve_nats_url(args.nats_url, dict(os.environ))
        try:
            headers = parse_headers(args.header)
        except ValueError as exc:
            print(str(exc), file=err)
            return 5
        try:
            body = asyncio.run(
                fetch_description(url, args.service, args.namespace, args.timeout, headers)
            )
        except (NoRespondersError, NatsTimeoutError):
            print(
                f"no service {args.service!r} answered on the describe subject within "
                f"{args.timeout}s at {url}. Check --service and --namespace.",
                file=err,
            )
            return 2
        except (ConnectionRefusedError, OSError, NoServersError) as exc:
            print(
                f"no broker reachable at {url}: {exc}. Use --nats-url or CLIFFRACER_NATS_URL.",
                file=err,
            )
            return 3
        except Exception as exc:  # noqa: BLE001 - nats errors are not one base class
            print(f"no broker reachable at {url}: {exc}", file=err)
            return 3

        if "error" in body:
            # Handle error envelope if description was refused.
            reason = str(body["error"])
            hint = (
                ' Send credentials with --header authorization="bearer <token>".'
                if reason.startswith("refused: ")
                else ""
            )
            print(f"{args.service} would not describe itself: {reason}.{hint}", file=err)
            return 4
        desc = Description.from_dict(body)

    # Collect all unimportable models before code emission.
    offenders = _unimportable(desc)
    if offenders:
        print(
            "a generated client could not import: "
            + ", ".join(offenders)
            + ". Move these models into an importable package.",
            file=err,
        )
        return 4

    try:
        source = emit(desc)
    except CannotEmit as exc:
        print(str(exc), file=err)
        return 4

    if args.out:
        Path(args.out).write_text(source)
    else:
        sys.stdout.write(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

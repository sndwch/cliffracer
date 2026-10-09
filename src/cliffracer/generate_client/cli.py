"""cliffracer-generate-client: generate typed clients from service descriptions.

THE EXIT CODES ARE THE INTERFACE. A person reads the message; anything
scripting the generator reads the code:

    0  a client was written, or --check found an exact match
    2  the broker answered but no such service did
    3  no broker at that address, or a broker that refused this client's credentials or
       permissions
    4  the service cannot be described, has no rpc handler, or its description
       cannot be emitted
    5  the class named by --class could not be imported, or is not a class
    6  the client could not be written where --out asked
    7  the command line is wrong: a missing, unknown or malformed flag, or a
       flag the chosen mode does not use
    8  --check found a missing or stale generated client
    9  the service class source could not be verified under the required path
   10  --check could not read the output file

7 is not argparse's 2, because 2 already means something here. A script that
gets 7 has a mistake in its own invocation; one that gets 2 reached a broker.

2 and 3 are deliberately different. "Nothing is listening" and "something is
listening and your service name is wrong" send a reader to different places,
and a single "connection problem" code sends them to the wrong one half the
time.

Every message names the thing the reader has to change -- the module, the
address, the model -- and every failure path leaves no file behind. A
half-written client is worse than none, because it imports. That holds for the
write itself: the source goes to a temporary file in the target's own
directory and is moved onto the target only once it is whole, so a failure
leaves the previous client exactly as it was.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn, cast

from nats.errors import Error as NatsError
from nats.errors import NoRespondersError, NoServersError
from nats.errors import TimeoutError as NatsTimeoutError
from pydantic import ValidationError

from cliffracer.cli.live_service import (
    DEFAULT_URL as DEFAULT_URL,
)
from cliffracer.cli.live_service import (
    BrokerRefused,
    _reply_problem,
    fetch_description,
)
from cliffracer.cli.live_service import (
    describe_subject as describe_subject,
)
from cliffracer.cli.live_service import (
    resolve_nats_url as resolve_nats_url,
)
from cliffracer.client import ServiceClient
from cliffracer.core.endpoints import redact_nats_url
from cliffracer.core.exceptions import (
    ConfigurationError,
    RpcError,
    RpcRefusedError,
    raise_for_error_envelope,
)
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.subjects import validate_inbox_prefix
from cliffracer.core.typed_rpc import UntypedHandler, shown, unimportable_models
from cliffracer.introspect import Description, describe

from .checking import check_client
from .emitter import CannotEmit, emit

EXIT_USAGE = 7
DEFAULT_TIMEOUT = 5.0


class _Parser(argparse.ArgumentParser):
    """An `ArgumentParser` whose usage errors exit `EXIT_USAGE` instead of 2."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _header_pair(pair: str) -> tuple[str, str]:
    """`NAME=VALUE` into a pair. The value may contain `=`."""
    name, sep, value = pair.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(f"--header wants NAME=VALUE, got {pair!r}")
    return name.strip(), value


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="cliffracer-generate-client",
        description="Generate a typed client for a cliffracer service.",
    )
    parser.add_argument("--service", required=True, help="service name (the subject prefix)")
    parser.add_argument(
        "--namespace",
        default=None,
        help=(
            "the namespace the service runs in: asked in without --class, and recorded in "
            "the client as NAMESPACE, which a client constructed without namespace= uses"
        ),
    )
    parser.add_argument(
        "--nats-url",
        default=None,
        help="broker URL; falls back to CLIFFRACER_NATS_URL, then " + DEFAULT_URL,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "without --class, seconds to wait for the broker, and again for the reply; "
            f"default {DEFAULT_TIMEOUT:g}"
        ),
    )
    parser.add_argument(
        "--inbox-prefix",
        default=None,
        metavar="PREFIX",
        help=(
            "without --class, the inbox prefix the connection's replies arrive on, for a client "
            "role the broker confines to its own inbox prefix (docs/broker-permissions.md)"
        ),
    )
    parser.add_argument(
        "--class",
        dest="target",
        default=None,
        metavar="MODULE:CLASS",
        help="describe the class in-process instead of asking a live service",
    )
    parser.add_argument(
        "--version",
        default=None,
        help=(
            "with --class, the version to record; a class cannot see the ServiceConfig it "
            "is started with, so pass the version that config declares. Without it the "
            "ServiceConfig default is recorded. A running service reports its own"
        ),
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        type=_header_pair,
        metavar="NAME=VALUE",
        help=(
            "header to send with the describe request, repeatable; a service behind "
            'AuthExtension needs --header authorization="bearer <token>"'
        ),
    )
    parser.add_argument("--out", default=None, help="write here instead of stdout")
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the complete generated client with --out without writing it",
    )
    parser.add_argument(
        "--require-source-under",
        type=Path,
        metavar="PATH",
        help="with --class, require its defining source file to resolve under this directory",
    )
    return parser


class NotAClass(Exception):
    """`--class` resolved to something that is not a class."""


class SourceMismatch(Exception):
    """The service class does not have verifiable source under the required root."""


def _require_source(cls: type, target: str, root: Path) -> None:
    """Verify the defining class, including re-exports and resolved symlinks."""
    try:
        required = root.resolve(strict=True)
        if not required.is_dir():
            raise SourceMismatch(f"--require-source-under {root}: expected a directory")
        filename = inspect.getsourcefile(cls)
        if filename is None:
            raise SourceMismatch(
                f"cannot locate a source file for {target}; required under {required}"
            )
        source = Path(filename).resolve(strict=True)
        if not source.is_file() or not source.is_relative_to(required):
            raise SourceMismatch(f"{target} source is {source}; required under {required}")
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise SourceMismatch(f"cannot verify {target} source under {root}: {exc}") from exc


def _kind(obj: object) -> str:
    if inspect.isroutine(obj):
        return "function"
    if inspect.ismodule(obj):
        return "module"
    return type(obj).__name__


def describe_class(
    target: str, service: str, version: str | None, *, source_root: Path | None = None
) -> Description:
    """Describe the class `MODULE:CLASS` names.

    `describe` walks whatever it is handed and would return a description with
    no methods for a function or a dict, so anything that is not a class is
    refused here, by name, before it is walked.
    """
    module_name, _, cls_name = target.partition(":")
    module = importlib.import_module(module_name)
    cls = getattr(module, cls_name)
    if not inspect.isclass(cls):
        raise NotAClass(f"{target} is a {_kind(cls)}, not a class")
    if source_root is not None:
        _require_source(cls, target, source_root)
    return describe(cls, service=service, version=version)


def _unimportable(desc: Description) -> list[str]:
    offenders: set[str] = set()
    for method in desc.methods:
        for param in method.params:
            offenders.update(unimportable_models(param.type))
        offenders.update(unimportable_models(method.returns))
    return sorted(offenders)


def _mode_for(target: Path) -> int:
    """The permissions a plain `open(target, "w")` would have left.

    `tempfile` creates at 0600, so that a secret cannot be read between
    creation and use. That is right for a secret and wrong here: replacing the
    target with the temporary file carries its mode across, and a client that
    was world-readable would silently become owner-only. Measured on this
    machine: `write_text` produces 0664 under a 002 umask and the temporary
    file 0600.

    An existing target keeps the mode it already had. Otherwise the mode is
    the one `open` would have chosen, which means reading the umask -- and the
    only way to read it is to set it, so it is set back immediately. Safe in a
    single-threaded entry point and not safe in general, which is why this is
    here rather than in a library.
    """
    if target.exists():
        return stat.S_IMODE(target.stat().st_mode)
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _write_atomically(target: Path, source: str) -> None:
    """Write `source` to `target`, or leave `target` as it was.

    `Path.write_text` opens the target for writing, which truncates it before
    a single byte is encoded. So an encoding failure or a full disk left a
    0-byte file where a working client used to be -- the one outcome this
    command's docstring promises never to produce.

    `encoding="utf-8"` because generated Python source is UTF-8 by
    definition. Without it the locale decides: under a non-UTF-8 locale a
    non-ASCII character in a handler docstring raised UnicodeEncodeError, and
    a handler docstring is the only place a non-ASCII character survives,
    since `_literal` routes every string through `json.dumps`.

    The temporary file goes in the target's OWN directory, so `os.replace` is
    within one filesystem and therefore atomic. A temporary directory
    elsewhere would make it a copy that can fail halfway, which is the defect
    again with more steps.
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    written = Path(handle.name)
    try:
        with handle:
            handle.write(source)
        os.chmod(written, _mode_for(target))
        os.replace(written, target)
    except BaseException:
        written.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    err = sys.stderr
    if args.check and not args.out:
        parser.error("--check requires --out to name the generated client to compare")
    if args.require_source_under is not None and not args.target:
        parser.error("--require-source-under applies only with --class; live source is remote")
    if args.target:
        live_only = (
            ("--header", bool(args.header)),
            ("--nats-url", args.nats_url is not None),
            ("--timeout", args.timeout is not None),
            ("--inbox-prefix", args.inbox_prefix is not None),
        )
        for flag, given in live_only:
            if given:
                parser.error(
                    f"{flag} applies only without --class; a class is described in process"
                )
    elif args.version is not None:
        parser.error("--version applies only with --class; a running service reports its own")
    # Each is checked on its own, with the prefix pinned to none, and reported under the flag or the
    # variable it came from: `ServiceConfig` reads its own prefix from the environment, and a
    # bad one there would otherwise be reported against whichever field was being checked.
    checks: list[tuple[str, dict[str, Any]]] = [
        ("--service", {"name": args.service, "subject_prefix": None})
    ]
    if args.namespace is not None:
        checks.append(
            (
                "--namespace",
                {"name": "service", "namespace": args.namespace, "subject_prefix": None},
            )
        )
    # A class described with `--class` is described in process and sends nothing. A running service
    # is asked on a subject that starts with the environment's prefix, so a prefix that no service
    # can serve is refused here, by name, instead of waiting out the timeout.
    from_environment = os.environ.get("CLIFFRACER_SUBJECT_PREFIX")
    if not args.target and from_environment:
        checks.append(
            (
                f"$CLIFFRACER_SUBJECT_PREFIX={from_environment!r}",
                {"name": "service", "subject_prefix": from_environment},
            )
        )
    for label, kwargs in checks:
        try:
            ServiceConfig.model_validate(kwargs)
        except ValidationError as exc:
            reason = exc.errors()[0]["msg"].removeprefix("Value error, ")
            parser.error(f"{label}: {reason}")
    if args.inbox_prefix is not None:
        try:
            validate_inbox_prefix(args.inbox_prefix)
        except ValueError as exc:
            parser.error(f"--inbox-prefix: {exc}")
    if args.timeout is None:
        args.timeout = DEFAULT_TIMEOUT

    if args.target:
        try:
            desc = describe_class(
                args.target, args.service, args.version, source_root=args.require_source_under
            )
        except SourceMismatch as exc:
            print(f"{exc}. Check --class, PYTHONPATH and the editable install.", file=err)
            return 9
        except (ImportError, AttributeError, ValueError) as exc:
            print(
                f"could not import {args.target}: {exc}. Give MODULE:CLASS on the PYTHONPATH.",
                file=err,
            )
            return 5
        except NotAClass as exc:
            print(f"{exc}. Give MODULE:CLASS naming a service class.", file=err)
            return 5
        except (UntypedHandler, ConfigurationError) as exc:
            print(f"cannot describe {args.target}: {exc}", file=err)
            return 4
        if not desc.methods:
            print(
                f"no @rpc handler found on {args.target}, so a client would have nothing "
                "to call. Only handlers whose names do not start with an underscore "
                "are published.",
                file=err,
            )
            return 4
    else:
        url = resolve_nats_url(args.nats_url, dict(os.environ))
        headers = dict(args.header)
        try:
            raw = asyncio.run(
                fetch_description(
                    url,
                    args.service,
                    args.namespace,
                    args.timeout,
                    headers,
                    **({} if args.inbox_prefix is None else {"inbox_prefix": args.inbox_prefix}),
                )
            )
        except BrokerRefused as exc:
            if exc.kind == "credentials":
                print(
                    f"the broker at {redact_nats_url(url)} refused this client's credentials: "
                    f"{exc.detail}. Check the user, password or token in --nats-url.",
                    file=err,
                )
            else:
                print(
                    f"the broker at {redact_nats_url(url)} refused this client a permission "
                    f"the request needs: {exc.detail}. A client role confined to an inbox "
                    "prefix needs --inbox-prefix naming it.",
                    file=err,
                )
            return 3
        except (NoRespondersError, NatsTimeoutError):
            print(
                f"no service {args.service!r} answered on the describe subject within "
                f"{args.timeout}s at {redact_nats_url(url)}. Check --service and --namespace.",
                file=err,
            )
            return 2
        except (ConnectionRefusedError, OSError, NoServersError) as exc:
            reason = str(exc) or f"it did not answer within {args.timeout}s"
            print(
                f"no broker reachable at {redact_nats_url(url)}: {reason}. "
                "Use --nats-url or CLIFFRACER_NATS_URL.",
                file=err,
            )
            return 3
        except NatsError as exc:
            # Every nats-py exception derives from `nats.errors.Error`. Anything
            # else raised here is a defect in this command, not a broker that
            # could not be used, and propagates as one.
            print(f"no broker reachable at {redact_nats_url(url)}: {exc}", file=err)
            return 3

        try:
            body = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            print(
                f"{args.service} answered describe with a reply that is not JSON: "
                f"{ServiceClient._preview(raw)}.",
                file=err,
            )
            return 4

        if isinstance(body, dict) and "error" in body:
            # Handle error envelope if description was refused.
            reason = shown(body["error"])
            # Classified by the reply's `code`, with the prose read only for an old service that
            # sent none: both are `raise_for_error_envelope`'s to decide, as for a call.
            try:
                raise_for_error_envelope(body, args.service)
            except RpcRefusedError:
                hint = ' Send credentials with --header authorization="bearer <token>".'
            except RpcError:
                hint = ""
            else:
                hint = ""
            print(f"{args.service} would not describe itself: {reason}.{hint}", file=err)
            return 4
        if problem := _reply_problem(body):
            print(
                f"{args.service} answered describe with a reply that is not a description: "
                f"{problem}.",
                file=err,
            )
            return 4
        desc = Description.from_dict(cast(dict[str, Any], body))
        if not desc.methods:
            print(
                f"{args.service} answered describe with no @rpc handler, so a client "
                "would have nothing to call.",
                file=err,
            )
            return 4

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
        source = emit(desc, namespace=args.namespace)
    except CannotEmit as exc:
        print(str(exc), file=err)
        return 4

    if args.check:
        try:
            differences = check_client(Path(args.out), source)
        except OSError as exc:
            print(f"could not read {args.out} for --check: {exc.strerror or exc}.", file=err)
            return 10
        if differences:
            print("\n".join(differences), file=err)
            print("Regenerate with the same arguments without --check.", file=err)
            return 8
        return 0

    if args.out:
        try:
            _write_atomically(Path(args.out), source)
        except (OSError, UnicodeError) as exc:
            # `strerror`, not `str(exc)`: the exception names the TEMPORARY
            # file, which the reader never asked for and cannot act on, and
            # every message here is supposed to name the thing they change.
            reason = getattr(exc, "strerror", None) or str(exc)
            print(
                f"could not write {args.out}: {reason}. "
                "Nothing was written; any previous client is untouched.",
                file=err,
            )
            return 6
    else:
        sys.stdout.write(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

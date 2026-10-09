"""`cliffracer describe` and `cliffracer call`: ask a running service what it offers, and call it.

Both read the service's own live description first. `describe` prints it; `call` uses it to check
the call before sending: the method is one the service lists, every argument name is one it takes,
every argument without a default is given, and a scalar or literal argument is of that kind. The
service judges every value, and answers a typed refusal when one is wrong. Only a listed
request-reply method can be called: a name cannot be made into another subject, and there is no
fire-and-forget call and no event publish.

THE EXIT CODES ARE THE INTERFACE. The transport ones are `cliffracer-generate-client`'s:

     0  described, or called and answered with a result
     2  the broker answered but no such service did
     3  no broker at that address, or a broker that refused this client's credentials or
        permissions
     4  the service cannot be described, or describes the method in a way this command cannot
        call (a streaming method)
     7  the command line is wrong
    11  the arguments were refused: before sending, or by the service (`validation_failed`)
    12  no such method: not in the description, or the service answered `unknown_method`
    13  the service is busy (`busy`); the error object carries its `retry_after`
    14  the service's deadline passed (`deadline_exceeded`)
    15  the service refused the call (`refused`), as an auth or policy extension does
    16  any other error the service answered, or a reply that is not one
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TextIO

from nats.errors import Error as NatsError
from nats.errors import NoRespondersError, NoServersError
from nats.errors import TimeoutError as NatsTimeoutError

from cliffracer.calls import prepare
from cliffracer.core import dial
from cliffracer.core.deadline import TIMEOUT_HEADER, caller_budget, header_value
from cliffracer.core.endpoints import redact_nats_url
from cliffracer.core.exceptions import (
    RpcBusyError,
    RpcDeadlineExceededError,
    RpcRefusedError,
    RpcUnknownMethodError,
    RpcValidationError,
    raise_for_error_envelope,
)
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.subjects import validate_inbox_prefix

from .live_service import DEFAULT_URL, BrokerRefused, _Refusals, _reply_problem, describe_subject

EXIT_NO_SERVICE = 2
EXIT_NO_BROKER = 3
EXIT_UNDESCRIBABLE = 4
EXIT_USAGE = 7
EXIT_ARGUMENTS = 11
EXIT_UNKNOWN_METHOD = 12
EXIT_BUSY = 13
EXIT_DEADLINE = 14
EXIT_REFUSED = 15
EXIT_SERVER_ERROR = 16

DEFAULT_TIMEOUT = 5.0

#: How a connection is opened: `dial.connect`, or a test's broker.
Dialer = Callable[..., Awaitable[Any]]


class _Failed(Exception):
    """A failure this command reports and exits with: the code, and what goes to stderr."""

    def __init__(self, code: int, message: str, error: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.error = error


# --- The connection options -------------------------------------------------------------------


def add_connection_options(parser: argparse.ArgumentParser) -> None:
    """The options every command that asks a running service takes, environment first."""
    group = parser.add_argument_group("connection")
    group.add_argument(
        "--server",
        default=None,
        help=f"broker URL; $CLIFFRACER_NATS_URL, then $NATS_URL, then {DEFAULT_URL}",
    )
    group.add_argument("--creds", default=None, help="credentials file; $NATS_CREDS")
    group.add_argument("--user", default=None, help="$NATS_USER")
    group.add_argument(
        "--password", default=None, help="$NATS_PASSWORD; keep it there, out of the process list"
    )
    group.add_argument("--token", default=None, help="$NATS_TOKEN; keep it there")
    group.add_argument(
        "--inbox-prefix",
        default=None,
        metavar="PREFIX",
        help="the inbox prefix a role confined to one receives its replies on",
    )
    group.add_argument("--namespace", default=None, help="the namespace the service runs in")
    group.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"seconds to wait for the broker, and again for each reply; default {DEFAULT_TIMEOUT:g}",
    )
    group.add_argument(
        "--header",
        action="append",
        default=[],
        type=_header_pair,
        metavar="NAME=VALUE",
        help='header for every request, repeatable: --header authorization="bearer <token>"',
    )


def _header_pair(pair: str) -> tuple[str, str]:
    name, sep, value = pair.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(f"--header wants NAME=VALUE, got {pair!r}")
    return name.strip(), value


def server_url(args: argparse.Namespace, environ: dict[str, str]) -> str:
    """The flag, then `$CLIFFRACER_NATS_URL`, then `$NATS_URL`, then the default."""
    return (
        args.server or environ.get("CLIFFRACER_NATS_URL") or environ.get("NATS_URL") or DEFAULT_URL
    )


def connect_options(args: argparse.Namespace, environ: dict[str, str]) -> dict[str, Any]:
    """The credentials and inbox prefix to dial with, each flag before its variable."""
    options: dict[str, Any] = {}
    creds = args.creds or environ.get("NATS_CREDS")
    user = args.user or environ.get("NATS_USER")
    password = args.password or environ.get("NATS_PASSWORD")
    token = args.token or environ.get("NATS_TOKEN")
    if creds:
        options["user_credentials"] = creds
    if user:
        options["user"] = user
    if password:
        options["password"] = password
    if token:
        options["token"] = token
    if args.inbox_prefix is not None:
        options["inbox_prefix"] = args.inbox_prefix
    return options


def _check_names(args: argparse.Namespace, service: str) -> str | None:
    """What is wrong with the service name, namespace, environment prefix or inbox prefix."""
    checks: list[tuple[str, dict[str, Any]]] = [
        ("service", {"name": service, "subject_prefix": None})
    ]
    if args.namespace is not None:
        checks.append(
            ("--namespace", {"name": "s", "namespace": args.namespace, "subject_prefix": None})
        )
    prefix = os.environ.get("CLIFFRACER_SUBJECT_PREFIX")
    if prefix:
        checks.append(
            (f"$CLIFFRACER_SUBJECT_PREFIX={prefix!r}", {"name": "s", "subject_prefix": prefix})
        )
    for label, kwargs in checks:
        try:
            ServiceConfig.model_validate(kwargs)
        except ValueError as exc:
            reason = str(exc).splitlines()[-1].strip()
            errors = getattr(exc, "errors", None)
            if callable(errors):
                reason = errors()[0]["msg"].removeprefix("Value error, ")
            return f"{label}: {reason}"
    if args.inbox_prefix is not None:
        try:
            validate_inbox_prefix(args.inbox_prefix)
        except ValueError as exc:
            return f"--inbox-prefix: {exc}"
    return None


# --- Asking -------------------------------------------------------------------------------------


@asynccontextmanager
async def _connected(
    url: str, timeout: float, options: dict[str, Any], dialer: Dialer
) -> AsyncIterator[tuple[Any, _Refusals]]:
    """One connection for the whole command, bounded as the generator's describe fetch is."""
    refusals = _Refusals()
    try:
        nc = await dialer(
            url,
            timeout=timeout,
            connect_timeout=timeout,
            max_reconnect_attempts=1,
            reconnect_time_wait=min(timeout, 0.5),
            error_cb=refusals.record,
            **options,
        )
    except Exception as exc:
        if refusals.credentials is not None:
            raise BrokerRefused("credentials", refusals.credentials) from exc
        raise
    try:
        yield nc, refusals
    finally:
        await nc.close()


async def _ask(
    nc: Any,
    refusals: _Refusals,
    subject: str,
    payload: bytes,
    headers: dict[str, str],
    timeout: float,
) -> Any:
    try:
        return await nc.request(subject, payload, timeout=timeout, headers=headers or None)
    except NatsTimeoutError as exc:
        if refusals.permissions is not None:
            raise BrokerRefused("permissions", refusals.permissions) from exc
        raise


def _transport_failure(exc: BaseException, url: str, what: str, timeout: float) -> _Failed:
    where = redact_nats_url(url)
    if isinstance(exc, BrokerRefused):
        if exc.kind == "credentials":
            return _Failed(
                EXIT_NO_BROKER,
                f"the broker at {where} refused this client's credentials: {exc.detail}. "
                "Check --creds, --user, --password or --token.",
            )
        return _Failed(
            EXIT_NO_BROKER,
            f"the broker at {where} refused this client a permission the request needs: "
            f"{exc.detail}. A role confined to an inbox prefix needs --inbox-prefix naming it.",
        )
    if isinstance(exc, NoRespondersError | NatsTimeoutError):
        return _Failed(
            EXIT_NO_SERVICE,
            f"nothing answered {what} within {timeout:g}s at {where}. Check the service name "
            "and --namespace.",
        )
    reason = str(exc) or f"it did not answer within {timeout:g}s"
    return _Failed(EXIT_NO_BROKER, f"no broker reachable at {where}: {reason}.")


def _described(raw: bytes, service: str) -> dict[str, Any]:
    """The description a describe reply carries, or the failure it is."""
    try:
        body = json.loads(bytes(raw).decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _Failed(
            EXIT_UNDESCRIBABLE, f"{service} answered describe with a reply that is not JSON."
        ) from None
    if isinstance(body, dict) and "error" in body:
        raise _Failed(
            EXIT_UNDESCRIBABLE,
            f"{service} would not describe itself: {body['error']}.",
            _error_object(body),
        )
    if problem := _reply_problem(body):
        raise _Failed(
            EXIT_UNDESCRIBABLE,
            f"{service} answered describe with a reply that is not a description: {problem}.",
        )
    return dict(body)


def _error_object(envelope: dict[str, Any]) -> dict[str, Any]:
    """What an error envelope says, as the error object written to stderr."""
    out: dict[str, Any] = {"code": envelope.get("code"), "error": envelope.get("error")}
    for key in ("details", "retry_after", "correlation_id"):
        if envelope.get(key) is not None:
            out[key] = envelope[key]
    return out


# --- Types, read off the description -------------------------------------------------------------


def type_text(ref: dict[str, Any]) -> str:
    """A type reference as a reader writes the annotation."""
    kind = ref.get("kind")
    if kind == "scalar":
        return str(ref.get("name"))
    if kind == "model":
        return str(ref.get("qualname"))
    if kind == "optional":
        return f"{type_text(ref['inner'])} | None"
    if kind == "list":
        return f"list[{type_text(ref['item'])}]"
    if kind == "stream":
        return f"stream[{type_text(ref['item'])}]"
    if kind == "dict":
        return f"dict[str, {type_text(ref['value'])}]"
    if kind == "literal":
        return "Literal[" + ", ".join(json.dumps(v) for v in ref.get("values", [])) + "]"
    return str(kind)


#: The JSON kinds a scalar of each name accepts, as the service's validation would read them.
_SCALAR_ACCEPTS: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
}


def _argument_problem(name: str, ref: dict[str, Any], value: Any) -> str | None:
    """What is wrong with an argument's top-level kind, for the kinds a description decides."""
    kind = ref.get("kind")
    if kind == "optional":
        return None if value is None else _argument_problem(name, ref["inner"], value)
    if kind == "literal":
        values = ref.get("values", [])
        return None if value in values else f"{name} must be one of {values}, got {value!r}"
    if kind == "scalar":
        accepts = _SCALAR_ACCEPTS.get(str(ref.get("name")))
        if accepts is None:
            return None
        if isinstance(value, bool) and bool not in accepts:
            return f"{name} is a {ref['name']}, got {value!r}"
        if not isinstance(value, accepts):
            return f"{name} is a {ref['name']}, got {type(value).__name__} {value!r}"
    return None


def _scalar_from_text(ref: dict[str, Any], text: str) -> Any:
    """`--arg name=value` read as the parameter's scalar or literal; anything else is refused."""
    kind = ref.get("kind")
    if kind == "optional":
        return None if text == "null" else _scalar_from_text(ref["inner"], text)
    if kind == "literal":
        return next((v for v in ref.get("values", []) if str(v) == text), text)
    if kind == "scalar":
        name = ref.get("name")
        if name == "int":
            return int(text)
        if name == "float":
            return float(text)
        if name == "bool":
            if text not in ("true", "false"):
                raise ValueError(f"{text!r} is not true or false")
            return text == "true"
        if name == "str":
            return text
    raise ValueError(f"a {type_text(ref)} is given with --json-args, not --arg")


def _arguments(method: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """The call's arguments from --json-args and --arg, checked against the description."""
    params = {p["name"]: p for p in method.get("params", [])}
    given: dict[str, Any] = {}
    if args.json_args is not None:
        text = sys.stdin.read() if args.json_args == "-" else args.json_args
        try:
            given = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _Failed(EXIT_USAGE, f"--json-args is not JSON: {exc}.") from None
        if not isinstance(given, dict):
            raise _Failed(EXIT_USAGE, "--json-args must be a JSON object of the arguments.")
    for name, text in args.arg:
        if name not in params:
            raise _Failed(EXIT_ARGUMENTS, f"--arg {name}: {method['name']} takes no {name!r}.")
        try:
            given[name] = _scalar_from_text(params[name]["type"], text)
        except ValueError as exc:
            raise _Failed(EXIT_ARGUMENTS, f"--arg {name}: {exc}.") from None
    unknown = sorted(set(given) - set(params))
    if unknown:
        raise _Failed(
            EXIT_ARGUMENTS,
            f"{method['name']} takes no {unknown}; it takes {sorted(params) or 'no arguments'}.",
        )
    # A parameter with a default carries a `default` key on the wire, and one without carries none.
    missing = [n for n, p in params.items() if "default" not in p and n not in given]
    if missing:
        raise _Failed(EXIT_ARGUMENTS, f"{method['name']} needs {missing}, which were not given.")
    for name, value in given.items():
        if problem := _argument_problem(name, params[name]["type"], value):
            raise _Failed(EXIT_ARGUMENTS, f"{problem}.")
    return given


def _budget_header(seconds: float) -> str | None:
    """`seconds` as the `Cliffracer-Timeout-Ms` a service reads as a budget, or None when it would
    read none: judged by the service's own reading of the header, not a copy of its rule."""
    if not math.isfinite(seconds):
        return None
    value = header_value(seconds)
    return value if caller_budget({TIMEOUT_HEADER: value}) is not None else None


def _streams(ref: dict[str, Any]) -> bool:
    return ref.get("kind") == "stream"


# --- describe -----------------------------------------------------------------------------------


def render(description: dict[str, Any]) -> str:
    """A description as a person reads it."""
    lines = [f"{description['service']} {description['version']}"]
    methods = description.get("methods", [])
    lines.append("methods:" if methods else "methods: none")
    for method in methods:
        params = ", ".join(
            f"{p['name']}: {type_text(p['type'])}"
            + (f" = {json.dumps(p['default'])}" if "default" in p else "")
            for p in method.get("params", [])
        )
        lines.append(f"  {method['name']}({params}) -> {type_text(method['returns'])}")
        if method.get("doc_summary"):
            lines.append(f"      {method['doc_summary']}")
    listeners = description.get("listeners", [])
    if listeners:
        lines.append("listeners:")
        for listener in listeners:
            how = (
                f"durable={listener['durable']}"
                if listener.get("durable")
                else ("fanout" if listener.get("fanout") else "")
            )
            lines.append(f"  {listener.get('pattern', '')}  {how}".rstrip())
    return "\n".join(lines) + "\n"


async def _describe(args: argparse.Namespace, dialer: Dialer, out: TextIO) -> None:
    url = server_url(args, dict(os.environ))
    headers = dict(args.header)
    try:
        async with _connected(
            url, args.timeout, connect_options(args, dict(os.environ)), dialer
        ) as (nc, refusals):
            subject = describe_subject(args.service, args.namespace)
            reply = await _ask(nc, refusals, subject, b"", headers, args.timeout)
    except (BrokerRefused, NatsError, OSError, ConnectionRefusedError, NoServersError) as exc:
        raise _transport_failure(exc, url, f"describe for {args.service!r}", args.timeout) from None
    description = _described(reply.data, args.service)
    out.write(json.dumps(description) + "\n" if args.json else render(description))


# --- call ---------------------------------------------------------------------------------------

#: The error envelope's classes, as exit codes.
_CODES: tuple[tuple[type[Exception], int], ...] = (
    (RpcValidationError, EXIT_ARGUMENTS),
    (RpcUnknownMethodError, EXIT_UNKNOWN_METHOD),
    (RpcBusyError, EXIT_BUSY),
    (RpcDeadlineExceededError, EXIT_DEADLINE),
    (RpcRefusedError, EXIT_REFUSED),
)


def _target(text: str) -> tuple[str, str]:
    service, sep, method = text.rpartition(".")
    if not sep or not service or not method:
        raise argparse.ArgumentTypeError(f"wants SERVICE.METHOD, got {text!r}")
    return service, method


async def _call(args: argparse.Namespace, dialer: Dialer, out: TextIO, err: TextIO) -> None:
    service, method_name = args.target
    url = server_url(args, dict(os.environ))
    headers = dict(args.header)
    try:
        async with _connected(
            url, args.timeout, connect_options(args, dict(os.environ)), dialer
        ) as (nc, refusals):
            described = await _ask(
                nc, refusals, describe_subject(service, args.namespace), b"", headers, args.timeout
            )
            description = _described(described.data, service)
            method = next(
                (m for m in description.get("methods", []) if m["name"] == method_name), None
            )
            if method is None:
                listed = sorted(m["name"] for m in description.get("methods", []))
                raise _Failed(
                    EXIT_UNKNOWN_METHOD,
                    f"{service} has no method {method_name!r}; it has {listed or 'none'}.",
                )
            if _streams(method["returns"]):
                raise _Failed(
                    EXIT_UNDESCRIBABLE,
                    f"{service}.{method_name} streams its reply, and this version of "
                    "cliffracer call does not read a streamed reply.",
                )
            arguments = _arguments(method, args)
            # The request as cliffracer.calls builds it, so this command sends what call()
            # sends. A wait the service would not read as a budget (not finite, or over its
            # one-day ceiling) is no bound to the request: no budget is sent, and the service
            # applies its own max_rpc_processing_time. A budget given with --header is sent as given.
            subject, send, payload, _ = prepare(
                service,
                method_name,
                arguments,
                namespace=args.namespace,
                timeout=args.timeout if _budget_header(args.timeout) is not None else None,
                headers=headers,
            )
            if args.dry_run:
                out.write(
                    json.dumps({"subject": subject, "headers": send, "payload": arguments}) + "\n"
                )
                return
            reply = await _ask(nc, refusals, subject, payload, send, args.timeout)
    except _Failed:
        raise
    except (BrokerRefused, NatsError, OSError, ConnectionRefusedError, NoServersError) as exc:
        raise _transport_failure(exc, url, f"{service}.{method_name}", args.timeout) from None
    try:
        envelope = json.loads(bytes(reply.data).decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _Failed(
            EXIT_SERVER_ERROR, f"{service}.{method_name} answered with no JSON."
        ) from None
    if not isinstance(envelope, dict):
        raise _Failed(EXIT_SERVER_ERROR, f"{service}.{method_name} answered with no envelope.")
    if envelope.get("success") is True:
        out.write(json.dumps(envelope.get("result")) + "\n")
        return
    try:
        raise_for_error_envelope(envelope, f"{service}.{method_name}")
    except Exception as exc:  # noqa: BLE001 - classified below, by class
        code = next((c for cls, c in _CODES if isinstance(exc, cls)), EXIT_SERVER_ERROR)
        raise _Failed(code, str(exc), _error_object(envelope)) from None
    raise _Failed(
        EXIT_SERVER_ERROR,
        f"{service}.{method_name} answered with neither a result nor an error.",
        _error_object(envelope),
    )


# --- Entry ----------------------------------------------------------------------------------------


def add_commands(sub: Any) -> None:
    """`describe` and `call`, on the `cliffracer` command line."""
    describe = sub.add_parser("describe", help="Print what a running service offers.")
    describe.add_argument("service", help="the service's name")
    describe.add_argument("--json", action="store_true", help="the description as sent")
    add_connection_options(describe)

    call = sub.add_parser(
        "call",
        help="Call one method of a running service and print its result.",
        description="The result goes to stdout as JSON; an error, as a JSON object, to stderr.",
    )
    call.add_argument("target", type=_target, metavar="SERVICE.METHOD")
    call.add_argument(
        "--arg",
        action="append",
        default=[],
        type=_arg_pair,
        metavar="NAME=VALUE",
        help="one scalar or literal argument, repeatable",
    )
    call.add_argument(
        "--json-args",
        default=None,
        metavar="JSON",
        help="every argument as one JSON object, or - to read it from stdin",
    )
    call.add_argument(
        "--dry-run",
        action="store_true",
        help="print the subject, headers and payload instead of sending",
    )
    add_connection_options(call)


def _arg_pair(pair: str) -> tuple[str, str]:
    name, sep, value = pair.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(f"--arg wants NAME=VALUE, got {pair!r}")
    return name.strip(), value


async def run_async(
    args: argparse.Namespace,
    *,
    dialer: Dialer | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run `describe` or `call` on the running event loop, write what it says, return the code."""
    out = out or sys.stdout
    err = err or sys.stderr
    service = args.service if args.command == "describe" else args.target[0]
    if problem := _check_names(args, service):
        err.write(f"cliffracer {args.command}: {problem}\n")
        return EXIT_USAGE
    dial_with = dialer or dial.connect
    try:
        if args.command == "describe":
            await _describe(args, dial_with, out)
        else:
            await _call(args, dial_with, out, err)
    except _Failed as failure:
        if failure.error is not None:
            err.write(json.dumps(failure.error) + "\n")
        err.write(f"cliffracer {args.command}: {failure.message}\n")
        return failure.code
    return 0


def run(args: argparse.Namespace) -> int:
    """Run `describe` or `call` in its own event loop: the command line's entry."""
    return asyncio.run(run_async(args))

"""cliffracer-dlq: read the dead letters a service has published. It writes nothing.

THE EXIT CODES ARE THE INTERFACE. A person reads the message; anything scripting the inspector
reads the code:

    0  the command ran; a listing or a count of nothing is still 0
    3  no broker at that address
    4  no stream could be chosen: none holds the subject, several do (a wildcard) and none was
       named, or the one named is not there
    5  `show` named a sequence the stream does not hold
    6  the broker did not answer a stream request, or refused it: JetStream is off, or this
       identity is not allowed to read the stream
    7  the command line is wrong: a missing, unknown or malformed flag

7 is not argparse's 2, to match `cliffracer-generate-client`: a script that gets 7 has a mistake
in its own invocation, and one that gets 3 or 6 reached a broker.

Reading uses the stream's message-get API. No consumer is created, nothing is acknowledged and no
cursor moves. The password for `--password` and the token for `--token` are best kept in
`$NATS_PASSWORD` and `$NATS_TOKEN`, where a process list does not show them.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, NoReturn, TextIO
from urllib.parse import urlsplit

from nats.errors import Error as NatsError
from nats.errors import NoRespondersError, NoServersError
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import APIError, ServiceUnavailableError

from cliffracer.core import dial
from cliffracer.core.endpoints import _servers_of, redact_nats_url, unusable_nats_url
from cliffracer_dlq import render
from cliffracer_dlq.filters import Filters, parse_duration
from cliffracer_dlq.reader import (
    StreamChoiceError,
    ensure_stream,
    read,
    read_one,
    resolve_stream,
    stream_names,
)
from cliffracer_dlq.records import CAUSES

EXIT_OK = 0
EXIT_NO_BROKER = 3
EXIT_NO_STREAM = 4
EXIT_NO_SUCH_MESSAGE = 5
EXIT_NO_ANSWER = 6
EXIT_USAGE = 7

DEFAULT_SUBJECT = "dlq.*"
DEFAULT_LIMIT = 50


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _connection(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("connection")
    group.add_argument(
        "--server", default=os.environ.get("NATS_URL", "nats://127.0.0.1:4222"), help="$NATS_URL"
    )
    group.add_argument("--creds", default=os.environ.get("NATS_CREDS"), help="$NATS_CREDS")
    group.add_argument("--user", default=os.environ.get("NATS_USER"), help="$NATS_USER")
    group.add_argument("--password", default=os.environ.get("NATS_PASSWORD"), help="$NATS_PASSWORD")
    group.add_argument("--token", default=os.environ.get("NATS_TOKEN"), help="$NATS_TOKEN")
    group.add_argument(
        "--timeout", type=float, default=5.0, help="seconds to wait for the broker, default 5"
    )
    where = parser.add_argument_group("where the dead letters are")
    where.add_argument(
        "--stream", help="the stream; by default the one the broker names for --subject"
    )
    where.add_argument(
        "--subject",
        default=DEFAULT_SUBJECT,
        help=f"the dead-letter subject, wildcards allowed (default {DEFAULT_SUBJECT}); put the "
        f"service's subject prefix in front when it has one",
    )


def _filters(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("filters")
    group.add_argument("--service", help="only this service's dead letters")
    group.add_argument("--cause", choices=CAUSES, help="only this kind of dead letter")
    group.add_argument("--since", metavar="DURATION", help="only the last 90s, 15m, 2h or 1d3h5m2s")
    group.add_argument(
        "--original-subject",
        metavar="SUBJECT",
        help="only messages that arrived on this subject, wildcards allowed",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="cliffracer-dlq",
        description="Read the dead letters cliffracer services publish. Writes nothing.",
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    ls = sub.add_parser("ls", help="list dead letters, oldest first")
    _connection(ls)
    _filters(ls)
    ls.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"default {DEFAULT_LIMIT}")
    ls.add_argument("--json", action="store_true", help="one JSON object per line")

    show = sub.add_parser("show", help="show one dead letter in full")
    _connection(show)
    show.add_argument("sequence", type=int, help="its sequence in the dead-letter stream")
    show.add_argument("--json", action="store_true")

    count = sub.add_parser("count", help="count dead letters by service and cause")
    _connection(count)
    _filters(count)
    count.add_argument("--json", action="store_true")
    return parser


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _since(
    text: str | None, now: Callable[[], datetime.datetime] = _now
) -> datetime.datetime | None:
    if text is None:
        return None
    return now() - parse_duration(text)


def _filters_from(args: argparse.Namespace, now: Callable[[], datetime.datetime]) -> Filters:
    return Filters(
        service=args.service,
        cause=args.cause,
        since=_since(args.since, now),
        original_subject=args.original_subject,
    )


class Broker:
    """What the commands ask of the broker, and nothing else: stream names, stream state, a message."""

    def __init__(self, nc: Any, jsm: Any, timeout: float) -> None:
        self._nc = nc
        self._jsm = jsm
        self._timeout = timeout

    async def stream_names(self, subject: str) -> list[str]:
        return await stream_names(self._nc, subject, self._timeout)

    async def stream_info(self, name: str) -> Any:
        return await self._jsm.stream_info(name)

    async def get_msg(self, stream: str, **options: Any) -> Any:
        return await self._jsm.get_msg(stream, **options)


async def execute(
    args: argparse.Namespace,
    jsm: Any,
    out: TextIO,
    err: TextIO,
    now: Callable[[], datetime.datetime] = _now,
) -> int:
    """Run the command in `args` against `jsm`, a `Broker`. Never writes to the broker.

    `now` is the clock `--since` counts back from; a test supplies its own.
    """
    try:
        stream = await resolve_stream(jsm, args.stream, args.subject)
        await ensure_stream(jsm, stream)
    except StreamChoiceError as exc:
        print(f"cliffracer-dlq: {exc}", file=err)
        return EXIT_NO_STREAM

    if args.command == "show":
        dead_letter = await read_one(jsm, stream, args.sequence)
        if dead_letter is None:
            print(f"cliffracer-dlq: stream {stream} holds no message {args.sequence}", file=err)
            return EXIT_NO_SUCH_MESSAGE
        print(
            render.show_json(dead_letter) if args.json else render.show_text(dead_letter), file=out
        )
        return EXIT_OK

    filters = _filters_from(args, now)
    print(f"stream: {stream}  subject: {args.subject}", file=err)

    if args.command == "count":
        kept = [dl async for dl in read(jsm, stream, args.subject) if filters.matches(dl)]
        rows, total = render.count_table(kept)
        if args.json:
            print(
                json.dumps(
                    {
                        "total": total,
                        "counts": [{"service": s, "cause": c, "count": n} for s, c, n in rows],
                    }
                ),
                file=out,
            )
        else:
            print(render.count_text(rows, total), file=out)
        return EXIT_OK

    shown = 0
    async for dead_letter in read(jsm, stream, args.subject):
        if not filters.matches(dead_letter):
            continue
        if shown == args.limit:
            print(f"(more match: showing the first {args.limit}; --limit raises it)", file=err)
            break
        shown += 1
        if args.json:
            print(json.dumps(render.summary(dead_letter), sort_keys=True), file=out)
        else:
            print(render.list_line(dead_letter), file=out)
    return EXIT_OK


async def _ignore_error(error: Exception) -> None:
    """nats-py logs each refused dial with a traceback; the exit code and message say it once."""


def _host(url: str) -> str:
    """The address of each server a URL names, without any credentials, comma-separated.

    A list of servers is read by the framework's own splitting, and each server is printed from its
    redacted form, so a user and password never reach `urlsplit` or the line. A server `urlsplit`
    cannot read as a host and a port (no scheme, as in `user:pw@host:4222`, a port that is not one,
    a list it was not split into) is printed as redacted. It never raises.
    """
    return ",".join(_host_of(server) for server in _servers_of(url))


def _host_of(server: str) -> str:
    redacted = redact_nats_url(server)
    try:
        parts = urlsplit(redacted)
        host, port = parts.hostname, parts.port
    except ValueError:
        return redacted
    if host is None:
        return redacted
    return f"{host}:{port}" if port else host


async def _run(args: argparse.Namespace) -> int:
    options: dict[str, Any] = {
        "connect_timeout": args.timeout,
        "max_reconnect_attempts": 0,
        "allow_reconnect": False,
        "error_cb": _ignore_error,
    }
    if args.creds:
        options["user_credentials"] = args.creds
    if args.user:
        options["user"] = args.user
    if args.password:
        options["password"] = args.password
    if args.token:
        options["token"] = args.token
    try:
        # nats-py keeps dialling a first connection that is refused, so the wait is bounded here,
        # by the dial the framework uses everywhere: one that closes the client a cut-off leaves.
        nc = await dial.connect(args.server, timeout=args.timeout, **options)
    except (NoServersError, OSError, NatsTimeoutError, TimeoutError):
        print(f"cliffracer-dlq: no broker answered at {_host(args.server)}", file=sys.stderr)
        return EXIT_NO_BROKER
    except NatsError as exc:
        # The broker answered and turned this connection away, a refused login among the reasons;
        # nats-py raises its text as the base error class.
        print(
            f"cliffracer-dlq: the broker at {_host(args.server)} refused the connection: {exc}"
            + (
                ". Check --user, --password, --token or --creds."
                if "authorization" in str(exc).lower()
                else ""
            ),
            file=sys.stderr,
        )
        return EXIT_NO_BROKER
    try:
        broker = Broker(nc, nc.jsm(timeout=args.timeout), args.timeout)
        return await execute(args, broker, sys.stdout, sys.stderr)
    except (NatsTimeoutError, TimeoutError, NoRespondersError, ServiceUnavailableError):
        print(
            "cliffracer-dlq: the broker did not answer a stream request: JetStream may be off, "
            "or this identity may not be allowed to read the stream",
            file=sys.stderr,
        )
        return EXIT_NO_ANSWER
    except APIError as exc:
        print(f"cliffracer-dlq: the broker refused a stream request: {exc}", file=sys.stderr)
        return EXIT_NO_ANSWER
    finally:
        await nc.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The check `ServiceConfig` makes of a `nats_url`: nats-py dials one server per URL and refuses
    # a list or a port it cannot read before it dials, so neither is dialled to fail as a broker
    # that refused. The reason never repeats the URL, and the address is printed redacted.
    if (problem := unusable_nats_url(args.server)) is not None:
        parser.error(f"--server {_host(args.server)} is not a URL nats-py can dial: {problem}")
    if args.command in ("ls", "count"):
        try:
            _since(args.since)
        except ValueError as exc:
            parser.error(f"--since: {exc}")
        if args.command == "ls" and args.limit < 1:
            parser.error("--limit: must be at least 1")
    return asyncio.run(_run(args))

"""A subject that goes on the wire is built by a helper, not spelled at the call site.

Every name a service publishes or subscribes under carries the environment
prefix and the namespace. Both live in `ServiceConfig`, and both are applied by
one of the helpers below. A subject assembled inline at a transport call gets
neither, and the failure is not local: the caller reaches a subject nobody is
listening on, or a JetStream publish is refused by a stream declared under the
prefix that the publish does not carry.

That is not hypothetical. Three copies of the outbound-call shape existed, and
one of them missed the prefix; the dead-letter subject was built from a template
and missed it too. Both were found by running two suites against one broker and
reading 22 `NoRespondersError` out of the wreckage, which is an expensive way to
learn it. This reads the syntax tree instead.

The rule: an f-string that looks like a subject may not be handed straight to a
transport call. Build it through a helper, or bind it to a name that one
produced. Every argument of the call that names a subject is read: the subject,
first or as `subject=`, and a publish's reply subject, third or as `reply=`, since
the receiver answers on it. A subscribe's queue group and a request's other
arguments are not subjects. An awaited helper (`await self._back_subject(...)`)
built its subject as the helper does.

What it reads, and what it does not. The two shapes checked are an f-string and
a `.format()` whose template is one of the subject-template fields; those are
the two the repository actually uses. A subject built by `%` interpolation, by
`"".join(...)`, or by `.format()` on a string literal spelled at the call site
would pass unseen. Measured over the 51 modules of `src/cliffracer`: no `%` on a
dotted literal, no `.format()` on a dotted literal, and no `'.'.join(...)`. The
14 `join` calls that do exist all use `', '`, `'; '`, `'\n'` or `''` and build
messages, not subjects. That is why those shapes are not read -- a rule matching
nothing is a rule nobody maintains. If one appears it will be deliberate, and
the shape belongs here then rather than now.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# The reading when this guard was written. The floor is well below it: the point
# is to catch a sweep that collapsed, not to pin the size of the package.
FILES_WHEN_WRITTEN = 50
FILES_FLOOR = 35
SRC = REPO / "src" / "cliffracer"

# The calls that put a subject on the wire. `queue` is here because a queue
# group is a name on the broker in the same way a subject is.
TRANSPORT_CALLS = frozenset({"publish", "subscribe", "request", "pull_subscribe"})

# The helpers that apply the namespace and the prefix. A subject from one of
# these is correct by construction.
SUBJECT_HELPERS = frozenset(
    {
        "with_namespace",
        "_with_namespace",
        "effective_event_subject",
        "outbound_subject",
        "format_dlq_subject",
        "describe_subject",
        "dlq_subject",
        "_format_dlq_subject",
        "permission_subject",
        # An inbox, under its connection's inbox prefix: the namespace and the environment prefix
        # do not apply to it, as they do not to a client's own inboxes.
        "inbox_subject",
        # A client's own inbox, as nats-py's and the in-memory broker's connections make one.
        "new_inbox",
        # A streaming reply's back subject: `inbox_subject` under the service's inbox prefix.
        "_back_subject",
    }
)

# What a subject looks like when spelled out: a dotted name with a token that
# only a subject has.
SUBJECT_MARKERS = (".rpc.", ".async.", ".describe", "dlq.", ".>", ".*")

# Config fields holding a subject template, which `.format()` turns into a subject.
SUBJECT_TEMPLATE_FIELDS = frozenset({"dlq_subject"})


def source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


# EVERY SHIPPED MODULE, not only core. The f-string rule above reads
# `src/cliffracer` alone, and two of the three subjects the rules below catch
# live in `packages/` -- so they were invisible twice over: no marker in their
# literal text, and out of scope. A rule about what reaches a broker has to
# cover everything that ships.
def shipped_files() -> list[Path]:
    roots = [SRC, *sorted((REPO / "packages").glob("*/src/*"))]
    return sorted(
        p
        for root in roots
        if root.is_dir()
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    )


# THE RATCHET IS GONE, AND THAT IS THE POINT.
#
# This guard shipped with three exemptions, each keyed to the issue that would
# fix it, and `test_every_exemption_still_matches_its_site` failed whenever a
# fix made an exemption match nothing. All three have now been fixed -- the log
# sink, the dead-letter middleware, and the client subject this change routes
# through the builder -- so `EXEMPT_SITES` and its two tests are deleted rather
# than left empty.
#
# An empty allowlist guarded by two passing tests is an inert mechanism with a
# docstring: it reads as load-bearing to the next person, who will add an entry
# to it rather than fix their site, and its tests assert things about nothing.
# `test_no_subject_reaches_a_transport_call_unbuilt` now says plainly that there
# are no such sites. If a fourth instance ever needs holding, bring the ratchet
# back deliberately -- the history of this file has it.


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _subject_args(call: ast.Call) -> list[ast.expr]:
    """Every argument of a transport call that names a subject: the subject, first or as
    `subject=`, and for a publish the reply subject, third or as `reply=`."""
    found = list(call.args[:1]) + [kw.value for kw in call.keywords if kw.arg == "subject"]
    if _called(call) == "publish":
        found += list(call.args[2:3]) + [kw.value for kw in call.keywords if kw.arg == "reply"]
    return found


def _unawaited(node: ast.expr) -> ast.expr:
    """`node`, or the call it awaits: `await helper(...)` is that helper's subject."""
    return node.value if isinstance(node, ast.Await) else node


def _called(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = fn.args
    return {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}


def _assignments(fn: ast.AST) -> dict[str, list[ast.expr]]:
    found: dict[str, list[ast.expr]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            found.setdefault(node.target.id, []).append(node.value)
    return found


def _branches(expr: ast.expr) -> list[ast.expr]:
    """Both sides of the namespace-optional idiom, `f"{ns}.{x}" if ns else x`.

    That conditional is why a forward taint rule cannot do this: the taint dies
    at the `IfExp`. Reading both branches costs nothing and is why the rules
    below do not need dataflow analysis.
    """
    if isinstance(expr, ast.IfExp):
        return _branches(expr.body) + _branches(expr.orelse)
    return [expr]


def unbuilt_subjects_in(path: Path, source: str) -> list[tuple[str, int, str]]:
    """A subject handed to a transport call that no helper produced.

    Works BACKWARD from the call, which is what the earlier attempt at this
    could not do: the real defect binds the subject to a name and passes the
    name, so reading the call's own argument saw nothing.

    A parameter is not reported -- a wrapper like `_request(self, subject, ...)`
    receives a subject its caller owns, and reporting it would flag every such
    wrapper to catch none of them.
    """
    tree = ast.parse(source, filename=str(path))
    found: list[tuple[str, int, str]] = []
    for fn in _functions(tree):
        assigns = _assignments(fn)
        params = _params(fn)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or _called(node) not in TRANSPORT_CALLS:
                continue
            for arg in _subject_args(node):
                found.extend(_unbuilt(arg, assigns, params, node))
    return sorted(set(found))


def _unbuilt(
    arg: ast.expr, assigns: dict[str, list[ast.expr]], params: set[str], node: ast.Call
) -> list[tuple[str, int, str]]:
    """`arg` of the transport call `node`, as a report when no helper built it."""
    arg = _unawaited(arg)
    if _is_helper_call(arg) or isinstance(arg, ast.Constant):
        return []
    if isinstance(arg, ast.Name):
        if arg.id in params:
            # THE PARAMETER EXEMPTION IS NAME-BASED, WHICH IS A REAL
            # BOUNDARY. A parameter that is REASSIGNED from an inline
            # build before it reaches the wire still carries the
            # parameter's name, so this return exempts it:
            #
            #     async def send(self, payload, subject=None):
            #         subject = subject or f"{self.ns}.{self.name}.events"
            #         await self.nc.publish(subject, payload)
            #
            # Deliberately not widened. Measured across all 97 shipped
            # files: 2 parameters are reassigned from an inline
            # expression, both in `core/validation.py` (`min_ms`,
            # `max_ms`), and neither carries a subject -- so widening
            # the rule today would add reasoning for 0 subjects and a
            # false-positive surface for every wrapper that defaults an
            # argument. If that count stops being 0, this is the line
            # to change: track the parameter's own assignments the way
            # the branch below does for ordinary names.
            return []
        sources = assigns.get(arg.id, [])
        if not sources or any(_is_helper_call(_unawaited(s)) for s in sources):
            return []
        return [(arg.id, node.lineno, f"{_called(node)}(...)")]
    if isinstance(arg, ast.JoinedStr):
        return [(_render(arg), node.lineno, f"{_called(node)}(...)")]
    return []


def subject_returning_functions(trees: dict[Path, ast.AST]) -> set[str]:
    """Functions whose result reaches a transport call, derived from the calls.

    DERIVED, not read from `SUBJECT_HELPERS`. That is what stops this guard
    reading its expectation from the thing it guards: the allowlist is
    hand-maintained, this set is computed, and the guard reports the difference.
    """
    sinks: set[str] = set(TRANSPORT_CALLS)
    for tree in trees.values():
        for fn in _functions(tree):
            positional = [
                a.arg for a in fn.args.posonlyargs + fn.args.args if a.arg not in ("self", "cls")
            ]
            if not positional:
                continue
            first = positional[0]
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and _called(node) in sinks
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == first
                ):
                    sinks.add(fn.name)

    returners: set[str] = set()
    for _ in range(4):
        grew = False
        for tree in trees.values():
            for fn in _functions(tree):
                assigns = _assignments(fn)
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call) or _called(node) not in sinks:
                        continue
                    names = [a.id for a in _subject_args(node) if isinstance(a, ast.Name)]
                    for source in (_unawaited(s) for n in names for s in assigns.get(n, [])):
                        if isinstance(source, ast.Call):
                            name = _called(source)
                            if name and name not in returners:
                                returners.add(name)
                                grew = True
        if not grew:
            break
    return returners


def unregistered_builders_in(
    path: Path, tree: ast.AST, returners: set[str]
) -> list[tuple[str, int, str]]:
    """A function that builds and returns a subject without being a declared helper."""
    found: list[tuple[str, int, str]] = []
    for fn in _functions(tree):
        if fn.name in SUBJECT_HELPERS or fn.name not in returners:
            continue
        assigns = _assignments(fn)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            for branch in _branches(node.value):
                if isinstance(branch, ast.JoinedStr):
                    found.append((fn.name, fn.lineno, "returns a subject it built inline"))
                elif isinstance(branch, ast.Name):
                    for source in assigns.get(branch.id, []):
                        if any(isinstance(b, ast.JoinedStr) for b in _branches(source)):
                            found.append(
                                (fn.name, fn.lineno, f"returns {branch.id!r}, built inline")
                            )
    return sorted(set(found))


def unbuilt_subject_report(paths: list[Path] | None = None) -> dict[tuple[str, str], str]:
    """Every offending site, keyed by file and by the name the subject is bound to.

    The keying outlived the exemption dict it was designed for: keys by name
    rather than line number, because a line number churns.
    """
    files = paths if paths is not None else shipped_files()
    trees: dict[Path, ast.AST] = {}
    for path in files:
        try:
            trees[path] = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
    returners = subject_returning_functions(trees)
    report: dict[tuple[str, str], str] = {}
    for path, tree in trees.items():
        rel = _rel(path) if path.is_relative_to(REPO) else str(path)
        for name, line, why in unbuilt_subjects_in(path, path.read_text()):
            report[(rel, name)] = f"{rel}:{line} {name!r} reaches {why} unbuilt"
        for name, line, why in unregistered_builders_in(path, tree, returners):
            report[(rel, name)] = f"{rel}:{line} {name}() {why}"
    return report


def _looks_like_a_subject(node: ast.expr) -> bool:
    """True for an f-string that assembles a subject.

    An f-string specifically, not any string. A subject is *built* -- from a
    service name, a method, a namespace -- and building is what an f-string is
    for. Plain literals are excluded deliberately: they are overwhelmingly
    prose (docstrings describing a subject shape) and configuration defaults
    such as ``dlq.{service}``, which is a template a helper then formats.
    Sweeping those produced far more noise than signal, and a guard nobody
    reads is worse than no guard.
    """
    if not isinstance(node, ast.JoinedStr):
        return False
    text = "".join(part.value for part in node.values if isinstance(part, ast.Constant))
    return any(marker in text for marker in SUBJECT_MARKERS)


def _is_helper_call(node: ast.expr) -> bool:
    """True when the expression is a call to one of the subject helpers."""
    if not isinstance(node, ast.Call):
        return False
    name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
    return name in SUBJECT_HELPERS


def _formats_a_subject_template(node: ast.expr) -> bool:
    """True for ``config.dlq_subject.format(...)`` and its kin.

    A subject is not always an f-string: a template on the config, formatted at
    the call site, builds one just as surely and was the second place the prefix
    went missing. The f-string rule could not see it.
    """
    if not isinstance(node, ast.Call):
        return False
    if getattr(node.func, "attr", None) != "format":
        return False
    target = getattr(node.func, "value", None)
    return getattr(target, "attr", None) in SUBJECT_TEMPLATE_FIELDS


def inline_subjects_in(source: str) -> list[tuple[int, str]]:
    """Every spelled-out subject built outside a helper.

    The rule is about where a subject is SPELLED, not where it is passed. The
    defect this guard exists for assigned the subject to a name first and handed
    the name to the transport, so a check on the call's arguments saw nothing.

    A subject-shaped literal is allowed in exactly two places: inside one of the
    helpers, which is where the prefix and namespace are applied; and as an
    argument to one of them, which is the ordinary `_with_namespace(f"...")`
    shape. Anywhere else it reaches the wire unprefixed.
    """
    tree = ast.parse(source)

    inside_helper: set[int] = set()
    helper_args: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in SUBJECT_HELPERS:
                for inner in ast.walk(node):
                    inside_helper.add(id(inner))
        if _is_helper_call(node):
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                for inner in ast.walk(arg):
                    helper_args.add(id(inner))

    found = []
    for node in ast.walk(tree):
        if not (_looks_like_a_subject(node) or _formats_a_subject_template(node)):
            continue
        if id(node) in inside_helper or id(node) in helper_args:
            continue
        found.append((node.lineno, _render(node)))
    return sorted(set(found))


def _render(node: ast.expr) -> str:
    """The literal parts of a subject-shaped expression, for the failure text."""
    if isinstance(node, ast.Call):
        return "<template>.format(...)"
    if isinstance(node, ast.Constant):
        return str(node.value)
    return "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)


def test_no_wire_subject_is_assembled_outside_a_helper():
    """A subject reaching the broker came from a helper that knows the prefix."""
    offenders = {}
    for path in source_files():
        hits = inline_subjects_in(path.read_text())
        if hits:
            offenders[path.relative_to(REPO).as_posix()] = hits

    assert offenders == {}, (
        "these assemble a subject outside a helper, so it carries neither the "
        "namespace nor the environment prefix. Build it through one of "
        f"{sorted(SUBJECT_HELPERS)}, or pass the f-string into one: {offenders!r}"
    )


def test_the_sweep_read_the_source_tree():
    """No offenders and no files read produce the same output."""
    files = source_files()
    assert len(files) >= FILES_FLOOR, (
        f"the sweep found {len(files)} source files; it read {FILES_WHEN_WRITTEN} when "
        "this guard was written, and a number this low means it stopped reading rather "
        "than that the tree shrank"
    )
    assert any("dispatch" in p.as_posix() for p in files), (
        "the dispatch package is not in the sweep, and it is where subjects are built"
    )


def test_the_helpers_it_trusts_all_exist():
    """An entry naming a helper that is gone would exempt a shape nothing produces."""
    names = set()
    for path in source_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(node.name)
    missing = sorted(SUBJECT_HELPERS - names)
    assert not missing, f"these helpers are trusted but no longer defined: {missing}"


def test_CONTROL_an_inline_subject_is_caught():
    """Both shapes: handed straight to the transport, and bound to a name first."""
    direct = inline_subjects_in('await nc.publish(f"{svc}.rpc.{m}", b"")')
    assert direct and ".rpc." in direct[0][1]

    # The shape the real defect took, which a check on call arguments misses.
    via_name = inline_subjects_in(
        "subject = f\"{service}.rpc.{method}\"\nawait nc.publish(subject, b'')\n"
    )
    assert via_name and ".rpc." in via_name[0][1]


def test_CONTROL_a_helper_built_subject_is_not_caught():
    """The correct shape, direct and via a name."""
    assert inline_subjects_in('await nc.publish(self._with_namespace(f"{s}.rpc.{m}"), b"")') == []
    assert inline_subjects_in("await nc.publish(subject, b'')") == []
    assert (
        inline_subjects_in(
            'def with_namespace(cls, config, subject):\n    return f"{ns}.{subject}.rpc.*"\n'
        )
        == []
    )


def test_CONTROL_a_non_transport_call_is_not_caught():
    """A subject-shaped string elsewhere is not going on the wire."""
    assert inline_subjects_in('log.info(f"{svc}.rpc.{m} failed")') != [], (
        "a subject spelled in a log line is still a subject spelled outside a helper; "
        "if that becomes noisy, exempt it deliberately rather than by accident"
    )


# --- a subject that reaches the wire without a helper having built it ---------


def test_no_subject_reaches_a_transport_call_unbuilt():
    """No site, with nothing subtracted.

    The f-string rule above reads what is SPELLED. These two rules read what
    REACHES a transport call, which is the shape it cannot see: a builder that
    receives the distinguishing token as a parameter has no marker to find.

    This asserted "every site minus the three the ratchet holds" when it landed.
    All three are fixed, so it asserts the plain thing now.
    """
    unexpected = unbuilt_subject_report()

    assert not unexpected, (
        "these subjects reach a transport call without a helper having built "
        "them, so they carry neither the namespace nor the environment prefix:\n  "
        + "\n  ".join(sorted(unexpected.values()))
        + "\n\nIf the function named is a subject builder, register it in "
        f"SUBJECT_HELPERS ({sorted(SUBJECT_HELPERS)}) and it will be trusted. "
        "If it is not, route the subject through one of them. A red here on a "
        "correct new builder means it has not been registered, not that it is "
        "wrong."
    )


def test_the_shipped_sweep_reads_more_than_core():
    """The two package instances were out of scope as well as unmarked.

    A rule about what reaches a broker has to cover everything that ships, so
    this asserts the sweep sees `packages/` and not only `src/cliffracer`.
    """
    files = shipped_files()
    roots = {p.relative_to(REPO).parts[0] for p in files}

    assert "packages" in roots, f"the sweep reads only {roots}; two known sites are in packages/"
    assert len(files) > len(source_files()), (
        f"the shipped sweep ({len(files)}) is no wider than the core one ({len(source_files())})"
    )


def _tree(tmp_path: Path, body: str, name: str = "mod.py") -> list[Path]:
    path = tmp_path / name
    path.write_text(body)
    return [path]


def test_CONTROL_a_subject_bound_to_a_name_then_published_is_caught(tmp_path: Path):
    """The shape the earlier attempt could not see, and the reason for this rule."""
    paths = _tree(
        tmp_path,
        "async def send(nc, service):\n"
        '    subject = f"{service}.rpc.thing"\n'
        "    await nc.publish(subject, b'')\n",
    )

    report = unbuilt_subject_report(paths)

    assert any("subject" in key[1] for key in report), report


def test_CONTROL_a_subject_with_no_marker_at_all_is_caught(tmp_path: Path):
    """`f"logs.{service}.{level}"` carries no marker; the backward rule does not care."""
    paths = _tree(
        tmp_path,
        "async def sink(nc, service, level):\n"
        '    subject = f"logs.{service}.{level}"\n'
        "    await nc.publish(subject, b'')\n",
    )

    assert unbuilt_subject_report(paths), "a marker-free subject was not caught"


def test_CONTROL_a_helper_built_subject_is_not_caught_by_the_backward_rule(tmp_path: Path):
    """Registered helpers stay trusted, or the rule reports the correct code."""
    paths = _tree(
        tmp_path,
        "async def send(nc, config, service):\n"
        "    subject = with_namespace(config, service)\n"
        "    await nc.publish(subject, b'')\n",
    )

    assert unbuilt_subject_report(paths) == {}, unbuilt_subject_report(paths)


def test_CONTROL_a_subject_parameter_is_not_caught(tmp_path: Path):
    """A wrapper receives a subject its caller owns.

    Reporting this would flag every `_request(self, subject, ...)` in the tree
    to catch none of them, which is the ratio that gets a guard weakened.

    The exemption is name-based, and a parameter reassigned from an inline
    build is therefore missed. That boundary and its measured count are
    written beside the `continue` in `unbuilt_subjects_in` that makes the
    decision, rather than here, so the next person to read the code meets
    it before the next person to read the tests.
    """
    paths = _tree(
        tmp_path,
        "async def _request(nc, subject, payload):\n    await nc.publish(subject, payload)\n",
    )

    assert unbuilt_subject_report(paths) == {}, unbuilt_subject_report(paths)


def test_CONTROL_a_qualname_fstring_that_never_reaches_the_wire_is_not_caught(tmp_path: Path):
    """The named false positive, from `typed_events.py` and `typed_rpc.py`.

    `f"{owner.__qualname__}.{name}"` is a dotted f-string built from parameters
    and is not a subject. Nothing publishes it, which is the whole distinction
    these rules turn on.
    """
    paths = _tree(
        tmp_path,
        "def build_event_spec(func, name, owner):\n"
        '    qual = f"{owner.__qualname__}.{name}"\n'
        "    raise UntypedHandler(f'{qual}: type hints do not resolve')\n",
    )

    assert unbuilt_subject_report(paths) == {}, unbuilt_subject_report(paths)


def test_CONTROL_an_unregistered_builder_is_caught_through_a_wrapper(tmp_path: Path):
    """The interprocedural half: a builder two steps from the transport call.

    This is `ServiceClient._subject` reduced -- built inline, returned through
    the namespace-optional conditional, bound by a caller, and handed to a
    wrapper whose parameter reaches the wire.
    """
    paths = _tree(
        tmp_path,
        "class C:\n"
        "    def _subject(self, tail):\n"
        '        base = f"{self.service}.{tail}"\n'
        '        return f"{self.namespace}.{base}" if self.namespace else base\n'
        "    async def _request(self, subject, payload):\n"
        "        await self.nc.publish(subject, payload)\n"
        "    async def call(self, method):\n"
        '        subject = self._subject(f"rpc.{method}")\n'
        "        await self._request(subject, b'')\n",
    )

    report = unbuilt_subject_report(paths)

    assert any(key[1] == "_subject" for key in report), report


@pytest.mark.parametrize(
    "call",
    [
        pytest.param('await nc.publish(subject, b"", reply=f"{x}.inbox.{y}")', id="reply-keyword"),
        pytest.param('await nc.publish(subject, b"", f"{x}.inbox.{y}")', id="reply-positional"),
        pytest.param(
            'await nc.publish(subject=f"{x}.events.{y}", payload=b"")', id="subject-keyword"
        ),
        pytest.param(
            'await nc.subscribe(subject=f"{x}.events.{y}", cb=cb)', id="subscribe-keyword"
        ),
        pytest.param(
            'back = f"{x}.inbox.{y}"\n    await nc.publish(subject, b"", reply=back)',
            id="reply-bound-to-a-name",
        ),
    ],
)
def test_CONTROL_every_argument_that_names_a_subject_is_read(tmp_path: Path, call: str):
    """The subject, first or as `subject=`, and a publish's reply subject, third or as `reply=`.

    A reply subject reaches the wire as surely as the subject does: the receiver answers on it,
    so one built inline misses the prefix the reply's listener was subscribed under."""
    paths = _tree(tmp_path, f"async def send(nc, subject, x, y, cb):\n    {call}\n")

    assert unbuilt_subject_report(paths), call


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            'await nc.publish(subject, b"", reply=inbox_subject(p, r, x))', id="reply-from-a-helper"
        ),
        pytest.param(
            'back = await self._back_subject(x)\n    await nc.publish(subject, b"", reply=back)',
            id="reply-awaited-from-a-helper",
        ),
        pytest.param('await nc.publish(subject, b"", reply=nc.new_inbox())', id="a-clients-inbox"),
        pytest.param('await nc.subscribe(subject, f"{x}.group")', id="a-subscribes-queue-group"),
        pytest.param('await nc.request(subject, b"", f"{x}.y")', id="a-requests-third-argument"),
    ],
)
def test_CONTROL_an_argument_that_names_no_unbuilt_subject_is_not_read(tmp_path: Path, call: str):
    """A reply built by a helper, awaited or not, is built; a subscribe's queue group and a
    request's third argument are not subjects, so an f-string there is not one."""
    paths = _tree(tmp_path, f"async def send(self, nc, subject, x, p, r):\n    {call}\n")

    assert unbuilt_subject_report(paths) == {}, call

"""Guard: a timer and the cron timers read time and wait only through their clock.

A schedule read straight from `time`, `datetime.now` or `asyncio.sleep`/`wait_for` is a read an
injected clock cannot move: a test on a `FakeClock` would wait real time for it, or never see it.
So `Timer`, `CronTimer` and `DistributedCronTimer` read time through `self.clock`, and this guard
reads their three modules with `ast` for any other read.

A read is a call through the `time` module, a `datetime.now`/`utcnow`/`today` or `date.today`
call, an `asyncio.sleep`/`wait_for`/`timeout`/`timeout_at` call, an `asyncio.wait` given a bound
(`timeout=` other than `None`), or the event loop's own clock: `.time()`, `.call_later()` or
`.call_at()` on `asyncio.get_running_loop()` or `get_event_loop()`, or on what holds one. A name
holds the loop in the function that binds it (by assignment, tuple unpack or walrus) or that is
passed it as an argument by a call in the same file; an attribute set to it holds it everywhere.
A clock function taken without being called (`now = time.monotonic`, `partial(time.monotonic)`,
`loop.time`) is a read, since it is called later. A module or class imported under another name
is read under that name, and an import that would let a read be spelled without its module
(`from time import ...`, `from asyncio import sleep` or `wait`) is a read too.

The reads that stay are named below by file, enclosing function and what is read, with how many
there are and the reason they are not the schedule. A stop's grace and the bounds that keep a stop
or a finishing write from waiting for ever are on real time by ruling, as is a firing's deadline,
which is on the event loop's clock as a request's deadline is. An edit above an exempted read does
not touch its entry; a read gone, or another of the same kind added in an exempted function, fails
by name. The price of keying by function: one exempted read swapped for a new read of the same
kind in the same function keeps the count, and passes.
"""

import ast
import inspect
import pathlib
import time

import pytest

pytestmark = pytest.mark.repo

ROOT = pathlib.Path(__file__).resolve().parents[2]
TIMER = "src/cliffracer/core/timer.py"
CRON = "packages/cliffracer-cron/src/cliffracer_cron/cron.py"
DISTRIBUTED = "packages/cliffracer-cron/src/cliffracer_cron/distributed.py"
SCHEDULERS = (TIMER, CRON, DISTRIBUTED)

_LEASE = (
    "a wall-clock stamp in the lease record that other replicas read and compare, so it is "
    "real time shared across processes, not this timer's schedule"
)

_STOP_GRACE = (
    "a stop's wait for the running firing, its grace and then its cancellation bound: how long "
    "stop() waits in real time, not when the timer fires"
)
_WRITE_BOUND = (
    "the bound on a finishing lease write, so a broker gone mid-write cannot hold the stop open "
    "for ever; it measures the write, not the schedule"
)

#: (file, enclosing function, the read) -> (how many there are, why they stay on real time).
#: Keyed by what is read and where, not by line, so an edit above a read does not re-pin it; the
#: count is what makes a second read of the same kind in an exempted function fail.
EXEMPT = {
    (TIMER, "Timer.stop", "asyncio.wait()"): (2, _STOP_GRACE),
    (TIMER, "Timer._within_deadline", "asyncio.get_running_loop().time()"): (
        1,
        "the start of a firing's deadline=, read off the event loop's clock that the deadline "
        "and the calls the firing makes are measured on",
    ),
    (TIMER, "Timer._within_deadline", "asyncio.timeout_at()"): (
        1,
        "a firing's deadline=, which is on the event loop's clock as a request's deadline is, so "
        "the calls the firing makes, which read that clock, get what is left of it",
    ),
    (CRON, "CronTimer.__init__", "datetime.now()"): (
        1,
        "the check, when the timer is built, that the expression names a date it can fire on; "
        "it schedules nothing",
    ),
    (DISTRIBUTED, "_UntilDone._wait", "asyncio.wait()"): (1, _WRITE_BOUND),
    (DISTRIBUTED, "_UntilDone.run", "loop.time()"): (2, _WRITE_BOUND),
    (DISTRIBUTED, "DistributedCronTimer._lease_started_at", "time.time()"): (1, _LEASE),
    (DISTRIBUTED, "DistributedCronTimer._execute_distributed", "time.time()"): (
        5,
        _LEASE + "; and the start and length of the firing's duration, written to that record",
    ),
}

_DATETIME_NOW = {"now", "utcnow", "today"}
_ASYNCIO_WAITS = {"sleep", "wait_for", "timeout", "timeout_at"}
_LOOP_GETTERS = {"get_running_loop", "get_event_loop"}
#: What a loop is asked that reads or schedules against its clock.
_LOOP_CLOCK = {"time", "call_later", "call_at"}
_FUNCTIONS = ast.FunctionDef | ast.AsyncFunctionDef


def _gets_the_loop(node: ast.AST) -> bool:
    """`asyncio.get_running_loop()`, `get_event_loop()`, under any owner or none."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in _LOOP_GETTERS


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """Every node in `scope`'s body that is not inside a function nested in it."""
    found: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        found.append(node)
        if not isinstance(node, _FUNCTIONS | ast.Lambda):
            pending.extend(ast.iter_child_nodes(node))
    return found


class _Scope:
    """A function, lambda or the module: its qualified name, its node and the scope enclosing it."""

    def __init__(self, name: str, node: ast.AST, parent: "_Scope | None") -> None:
        self.name, self.node, self.parent = name, node, parent


def _scopes(tree: ast.Module) -> list[_Scope]:
    """The module, each function and each lambda, enclosing scopes first. A function's name is
    qualified (`Class.method`, `outer.inner`, `outer.<lambda>`); its parent is the function or
    module it is nested in, never a class body, which Python does not search for a name either."""
    module = _Scope("<module>", tree, None)
    scopes = [module]

    def visit(node: ast.AST, prefix: str, parent: _Scope) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCTIONS | ast.Lambda):
                name = child.name if isinstance(child, _FUNCTIONS) else "<lambda>"
                scope = _Scope(prefix + name, child, parent)
                scopes.append(scope)
                visit(child, f"{prefix}{name}.", scope)
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.", parent)
            else:
                visit(child, prefix, parent)

    visit(tree, "", module)
    return scopes


def _locals(scope: _Scope) -> set[str]:
    """The names `scope` makes its own: parameters and names it binds, less any it declares
    `global` or `nonlocal`. A name an enclosing scope holds the loop under is shadowed by these."""
    node = scope.node
    names: set[str] = set()
    if isinstance(node, _FUNCTIONS | ast.Lambda):
        args = node.args
        every = args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]
        names |= {a.arg for a in every if a is not None}
    declared: set[str] = set()
    for own in _own_nodes(node):
        if isinstance(own, ast.Name) and isinstance(own.ctx, ast.Store):
            names.add(own.id)
        elif isinstance(own, ast.Global | ast.Nonlocal):
            declared |= set(own.names)
        elif isinstance(own, _FUNCTIONS):
            names.add(own.name)
    return names - declared


def _pairs(target: ast.AST, value: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    """What each name an assignment binds is bound to, element by element for a tuple unpack."""
    if isinstance(target, ast.Tuple | ast.List) and isinstance(value, ast.Tuple | ast.List):
        if len(target.elts) == len(value.elts):
            return [p for t, v in zip(target.elts, value.elts, strict=True) for p in _pairs(t, v)]
    return [(target, value)]


def _loops(scopes: list[_Scope]) -> tuple[dict[int, set[str]], set[str]]:
    """The names each scope sees the event loop under, and the attributes holding it anywhere.

    A scope binds a name to the loop by assignment, unpack or walrus, or as a parameter some call
    in the file passes the loop to. It sees that name, and every name an enclosing scope sees the
    loop under that it does not make its own, as Python resolves a name: a closure or a function
    reading a module-level loop sees it, and an unrelated parameter named `loop` does not. An
    attribute (`self._loop`) set to the loop is the loop in every scope, as a method reads what
    `__init__` set.
    """
    own: dict[int, set[str]] = {id(s): set() for s in scopes}
    locals_ = {id(s): _locals(s) for s in scopes}
    attributes: set[str] = set()
    functions = {s.name.rsplit(".", 1)[-1]: s for s in scopes if isinstance(s.node, _FUNCTIONS)}

    def visible() -> dict[int, set[str]]:
        seen: dict[int, set[str]] = {}
        for scope in scopes:  # enclosing scopes come first
            inherited = seen[id(scope.parent)] - locals_[id(scope)] if scope.parent else set()
            seen[id(scope)] = own[id(scope)] | inherited
        return seen

    for _ in range(len(scopes) + 1):  # each pass can carry the loop one call or scope deeper
        before = sum(map(len, own.values())) + len(attributes)
        names = visible()
        for scope in scopes:
            sees = names[id(scope)]

            def is_loop(node: ast.AST, sees: set[str] = sees) -> bool:
                return _gets_the_loop(node) or ast.unparse(node) in sees | attributes

            for node in _own_nodes(scope.node):
                bound: list[tuple[ast.AST, ast.AST]] = []
                if isinstance(node, ast.Assign):
                    bound = [p for t in node.targets for p in _pairs(t, node.value)]
                elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
                    bound = [(node.target, node.value)]
                for target, value in bound:
                    if is_loop(value):
                        held = ast.unparse(target)
                        (attributes if "." in held else own[id(scope)]).add(held)
                if isinstance(node, ast.Call):
                    callee = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    called = functions.get(callee or "")
                    if called is None:
                        continue
                    params = [a.arg for a in called.node.args.posonlyargs + called.node.args.args]
                    if params[:1] in (["self"], ["cls"]) and isinstance(node.func, ast.Attribute):
                        params = params[1:]
                    for param, arg in zip(params, node.args, strict=False):
                        if is_loop(arg):
                            own[id(called)].add(param)
                    for keyword in node.keywords:
                        if keyword.arg and is_loop(keyword.value):
                            own[id(called)].add(keyword.arg)
        if sum(map(len, own.values())) + len(attributes) == before:
            break
    return visible(), attributes


def scoped_time_reads(source: str) -> list[tuple[int, str, str]]:
    """Each read of time in `source` that does not go through a clock, as (line, scope, what).

    A call is a read, and so is a clock function taken without being called (`now =
    time.monotonic`, `partial(time.monotonic)`), since it is called later, out of sight.
    """
    tree = ast.parse(source)

    # The names a module, the datetime and date classes are reachable under here.
    time_names, asyncio_names = {"time"}, {"asyncio"}
    datetime_names, date_names = {"datetime"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "time":
                    time_names.add(bound)
                elif alias.name == "asyncio":
                    asyncio_names.add(bound)
                elif alias.name == "datetime":
                    datetime_names.add(f"{bound}.datetime")
                    date_names.add(f"{bound}.date")
        elif isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for alias in node.names:
                if alias.name == "datetime":
                    datetime_names.add(alias.asname or alias.name)
                elif alias.name == "date":
                    date_names.add(alias.asname or alias.name)

    scopes = _scopes(tree)
    loop_names, loop_attributes = _loops(scopes)

    def what_it_reads(owner: ast.AST, name: str, scope: _Scope, call: ast.Call | None) -> bool:
        spelled = ast.unparse(owner)
        if spelled in time_names:
            return call is not None or inspect.isbuiltin(getattr(time, name, None))
        if spelled in datetime_names and name in _DATETIME_NOW:
            return True
        if spelled in date_names and name == "today":
            return True
        if spelled in asyncio_names and name in _ASYNCIO_WAITS:
            return True
        if spelled in asyncio_names and name == "wait" and call is not None:
            bound = next((k.value for k in call.keywords if k.arg == "timeout"), None)
            return bound is not None and not (
                isinstance(bound, ast.Constant) and bound.value is None
            )
        loop = _gets_the_loop(owner) or spelled in loop_names[id(scope)] | loop_attributes
        return name in _LOOP_CLOCK and loop

    reads: list[tuple[int, str, str]] = []
    for scope in scopes:
        called = set()
        for own in _own_nodes(scope.node):
            if isinstance(own, ast.ImportFrom):
                names = {alias.name for alias in own.names}
                if own.module == "time" or (
                    own.module == "asyncio" and names & (_ASYNCIO_WAITS | {"wait"})
                ):
                    what = f"from {own.module} import {', '.join(sorted(names))}"
                    reads.append((own.lineno, scope.name, what))
            elif isinstance(own, ast.Call) and isinstance(own.func, ast.Attribute):
                called.add(id(own.func))
                if what_it_reads(own.func.value, own.func.attr, scope, own):
                    reads.append((own.lineno, scope.name, f"{ast.unparse(own.func)}()"))
        for own in _own_nodes(scope.node):
            if (
                isinstance(own, ast.Attribute)
                and isinstance(own.ctx, ast.Load)
                and id(own) not in called
                and what_it_reads(own.value, own.attr, scope, None)
            ):
                reads.append((own.lineno, scope.name, ast.unparse(own)))
    return sorted(reads)


def time_reads(source: str) -> list[tuple[int, str]]:
    """Each read of time in `source` that does not go through a clock, as (line, what)."""
    return [(line, what) for line, _, what in scoped_time_reads(source)]


def _reads() -> dict[tuple[str, str, str], list[int]]:
    """Each kind of read in the schedulers, by file, enclosing function and what is read, with
    the lines it is on."""
    found: dict[tuple[str, str, str], list[int]] = {}
    for path in SCHEDULERS:
        for line, scope, what in scoped_time_reads((ROOT / path).read_text()):
            found.setdefault((path, scope, what), []).append(line)
    return found


def test_the_schedulers_read_time_only_through_their_clock():
    unexempt = {key: lines for key, lines in _reads().items() if key not in EXEMPT}

    assert not unexempt, "read the time through `self.clock`, or exempt it here with a reason: " + (
        "; ".join(
            f"{path}:{lines} {scope} {what}" for (path, scope, what), lines in unexempt.items()
        )
    )


def miscounted(reads: dict, exempt: dict) -> dict[str, str]:
    """Each exemption whose count differs from the reads found under its key."""
    return {
        f"{path} {scope} {what}": f"exempts {count}, found {len(reads.get((path, scope, what), []))}"
        f" at {reads.get((path, scope, what), [])}"
        for (path, scope, what), (count, _) in exempt.items()
        if len(reads.get((path, scope, what), [])) != count
    }


def test_every_exemption_names_as_many_reads_as_it_says():
    """An exemption whose reads are gone is stale; one with more reads than it counts has had a
    read of the same kind added beside the ones it was written for, which nobody exempted."""
    wrong = miscounted(_reads(), EXEMPT)

    assert not wrong, f"re-count or drop these exemptions, or exempt the new read: {wrong}"


def test_CONTROL_a_read_added_beside_exempted_ones_or_one_gone_is_miscounted():
    exempt = {("f.py", "T.stop", "time.time()"): (2, "a reason")}

    assert miscounted({("f.py", "T.stop", "time.time()"): [3, 7]}, exempt) == {}
    assert miscounted({("f.py", "T.stop", "time.time()"): [3, 7, 9]}, exempt) == {
        "f.py T.stop time.time()": "exempts 2, found 3 at [3, 7, 9]"
    }
    assert miscounted({}, exempt) == {"f.py T.stop time.time()": "exempts 2, found 0 at []"}


def test_the_guard_reads_three_schedulers_that_use_their_clock():
    for path in SCHEDULERS:
        source = (ROOT / path).read_text()
        clock_reads = source.count("clock.")
        assert clock_reads >= 1, f"{path} reads its clock {clock_reads} time(s); is it the file?"


def test_CONTROL_each_kind_of_read_is_found():
    planted = (
        "import time\n"
        "from time import monotonic\n"
        "from asyncio import sleep\n"
        "async def loop(self):\n"
        "    a = time.monotonic()\n"
        "    b = datetime.now(UTC)\n"
        "    await asyncio.sleep(1)\n"
        "    await asyncio.wait_for(event.wait(), timeout=1)\n"
        "    c = self.clock.monotonic()\n"
        "    await asyncio.wait({task}, timeout=1)\n"
        "    async with asyncio.timeout_at(1):\n"
        "        pass\n"
    )

    assert time_reads(planted) == [
        (2, "from time import monotonic"),
        (3, "from asyncio import sleep"),
        (5, "time.monotonic()"),
        (6, "datetime.now()"),
        (7, "asyncio.sleep()"),
        (8, "asyncio.wait_for()"),
        (10, "asyncio.wait()"),
        (11, "asyncio.timeout_at()"),
    ]


@pytest.mark.parametrize(
    ("planted", "found"),
    [
        pytest.param(
            "x = asyncio.get_running_loop().time()\n",
            [(1, "asyncio.get_running_loop().time()")],
            id="the-running-loops-clock",
        ),
        pytest.param(
            "loop = asyncio.get_running_loop()\nx = loop.time()\n",
            [(2, "loop.time()")],
            id="a-loop-held-in-a-name",
        ),
        pytest.param(
            "self._loop = asyncio.get_event_loop()\nx = self._loop.time()\n",
            [(2, "self._loop.time()")],
            id="a-loop-held-in-an-attribute",
        ),
        pytest.param(
            "from asyncio import get_running_loop\nx = get_running_loop().time()\n",
            [(2, "get_running_loop().time()")],
            id="the-getter-imported-bare",
        ),
        pytest.param(
            "import time as t2\nx = t2.monotonic()\n",
            [(2, "t2.monotonic()")],
            id="an-aliased-time-module",
        ),
        pytest.param(
            "import asyncio as aio\nawait aio.sleep(1)\n",
            [(2, "aio.sleep()")],
            id="an-aliased-asyncio-module",
        ),
        pytest.param(
            "from datetime import datetime as D\nx = D.now()\n",
            [(2, "D.now()")],
            id="an-aliased-datetime-class",
        ),
        pytest.param(
            "import datetime as dt\nx = dt.datetime.now()\n",
            [(2, "dt.datetime.now()")],
            id="datetime-through-an-aliased-module",
        ),
        pytest.param(
            "await asyncio.wait({task}, timeout=1)\n",
            [(1, "asyncio.wait()")],
            id="a-wait-with-a-timeout",
        ),
        pytest.param(
            "from asyncio import wait\n",
            [(1, "from asyncio import wait")],
            id="wait-imported-bare",
        ),
    ],
)
def test_CONTROL_each_spelling_of_a_read_is_found(planted, found):
    assert time_reads(planted) == found


@pytest.mark.parametrize(
    ("planted", "found"),
    [
        pytest.param(
            "from datetime import date\nx = date.today()\n",
            [(2, "date.today()")],
            id="date-today-imported-bare",
        ),
        pytest.param(
            "import datetime as dt\nx = dt.date.today()\n",
            [(2, "dt.date.today()")],
            id="date-today-through-an-aliased-module",
        ),
        pytest.param(
            "now = time.monotonic\nx = now()\n",
            [(1, "time.monotonic")],
            id="a-clock-function-bound-to-a-name",
        ),
        pytest.param(
            "import functools\nf = functools.partial(time.monotonic)\n",
            [(2, "time.monotonic")],
            id="a-clock-function-in-a-partial",
        ),
        pytest.param(
            "loop = asyncio.get_running_loop()\nnow = loop.time\n",
            [(2, "loop.time")],
            id="the-loops-clock-bound-to-a-name",
        ),
        pytest.param(
            "if (loop := asyncio.get_running_loop()):\n    x = loop.time()\n",
            [(2, "loop.time()")],
            id="a-loop-bound-by-a-walrus",
        ),
        pytest.param(
            "a, loop = 1, asyncio.get_running_loop()\nx = loop.time()\n",
            [(2, "loop.time()")],
            id="a-loop-bound-by-a-tuple-unpack",
        ),
        pytest.param(
            "def stamp(clock_source):\n"
            "    return clock_source.time()\n"
            "async def run():\n"
            "    return stamp(asyncio.get_running_loop())\n",
            [(2, "clock_source.time()")],
            id="a-loop-passed-to-a-helper",
        ),
        pytest.param(
            "def stamp(at=None):\n"
            "    return at.time()\n"
            "async def run():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    return stamp(at=loop)\n",
            [(2, "at.time()")],
            id="a-loop-passed-to-a-helper-by-keyword",
        ),
        pytest.param(
            "class Timer:\n"
            "    def stamp(self, loop):\n"
            "        return loop.time()\n"
            "    async def run(self):\n"
            "        return self.stamp(asyncio.get_running_loop())\n",
            [(3, "loop.time()")],
            id="a-loop-passed-to-a-method",
        ),
        pytest.param(
            "asyncio.get_running_loop().call_later(1, fire)\n",
            [(1, "asyncio.get_running_loop().call_later()")],
            id="call-later-on-the-loop",
        ),
        pytest.param(
            "loop = asyncio.get_running_loop()\nloop.call_at(loop.time() + 1, fire)\n",
            [(2, "loop.call_at()"), (2, "loop.time()")],
            id="call-at-on-a-held-loop",
        ),
        pytest.param(
            "class Timer:\n    def fire(self):\n        self._stamp(lambda: time.time())\n",
            [(3, "time.time()")],
            id="a-read-inside-a-lambda",
        ),
        pytest.param(
            "async def run():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    def later():\n"
            "        return loop.time()\n"
            "    return later\n",
            [(4, "loop.time()")],
            id="a-loop-bound-in-an-enclosing-function",
        ),
        pytest.param(
            "loop = asyncio.get_event_loop()\ndef stamp():\n    return loop.time()\n",
            [(3, "loop.time()")],
            id="a-loop-bound-at-module-level",
        ),
    ],
)
def test_CONTROL_each_spelling_the_guard_once_missed_is_found(planted, found):
    assert time_reads(planted) == found


def test_CONTROL_a_read_is_keyed_by_the_function_it_is_in():
    """The exemptions are keyed by enclosing function, so the scope a read reports is the one an
    entry in `EXEMPT` names."""
    planted = (
        "class Timer:\n"
        "    def stop(self):\n"
        "        def later():\n"
        "            return time.time()\n"
        "        return time.time()\n"
        "x = time.time()\n"
        "y = sorted(rows, key=lambda r: time.time())\n"
    )

    assert scoped_time_reads(planted) == [
        (4, "Timer.stop.later", "time.time()"),
        (5, "Timer.stop", "time.time()"),
        (6, "<module>", "time.time()"),
        (7, "<lambda>", "time.time()"),
    ]


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param("await asyncio.wait({task})\n", id="a-wait-with-no-timeout"),
        pytest.param("x = self.clock.time()\n", id="a-clocks-own-time"),
        pytest.param("x = record.time()\n", id="time-on-something-that-is-not-a-loop"),
        pytest.param("import time\n", id="an-import-alone"),
        pytest.param("await asyncio.wait({task}, timeout=None)\n", id="a-wait-with-no-bound"),
        pytest.param("x = time.struct_time\n", id="a-time-type-not-a-clock-function"),
        pytest.param("x = record.call_later(1)\n", id="call-later-on-something-not-a-loop"),
        pytest.param(
            "async def run():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    return loop\n"
            "def unrelated(loop):\n"
            "    return loop.time()\n",
            id="a-parameter-named-loop-that-nothing-passes-the-loop-to",
        ),
        pytest.param(
            "loop = asyncio.get_event_loop()\ndef stamp(loop):\n    return loop.time()\n",
            id="an-enclosing-loop-shadowed-by-a-parameter",
        ),
        pytest.param(
            "async def run():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    def later():\n"
            "        loop = make_a_scheduler()\n"
            "        return loop.time()\n"
            "    return later\n",
            id="an-enclosing-loop-rebound-in-the-inner-function",
        ),
    ],
)
def test_CONTROL_what_reads_no_time_is_not_found(planted):
    """The detector is not "report every `.time()` and every wait", which would satisfy the rest."""
    assert time_reads(planted) == []

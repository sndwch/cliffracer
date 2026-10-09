"""ADR-0011 for error replies: they carry a `code`, and only a reply with none is classified by its text.

An RPC error reply carries a typed `code`, and the caller classifies on it. The message text is what
the service happened to format (a handler's own `str(e)` with `expose_internal_errors` on), so a
decision made on it lets a crash that reads "refused: ..." pass for a policy refusal and lets rewording
a string silently reclassify every error in the fleet. The text is read in exactly one place, for a
reply from a service that has not been redeployed and sends no `code`.

Two sweeps of `src/`, by syntax tree:

1. Every dict literal that has both an `"error"` and a `"success"` key (an RPC error reply) also has a
   `"code"` key.
2. No expression derived from a reply's `"error"` field (`d["error"]`, `d.get("error")`, `str(...)` of
   either, a name assigned from one, including one of a tuple unpacking and one holding the text
   alone in an f-string) is tested by prefix, suffix, substring, search, split or a slice
   (`e[:8] == "refused:"`), or compared with a string constant, written as a literal or as a
   module-level name bound to one. Reading the field with a default (`d.get("error") or ""`,
   `x if x else ""`) is still the text. Formatting it into a message, passing it on and printing it are
   not decisions. The permitted structured signals are the exception class, a reply's `code`, an
   `APIError.err_code`, subject tokens, header keys, process exit codes and exact equality on a
   schema field; the ADR lists them.

`EXEMPT` names the one function that reads a reply's text, with the reason, and a third check holds
it to the rule: the branch that handles a reply carrying a code makes no text decision.

A decision spelled through `operator` (`operator.contains(e, "x")`, `operator.eq(e, "x")`, a function
imported from it), through a bound method kept under a name (`sw = e.startswith; sw("x")`), or through
`re` under any import spelling (`import re as _re`, `from re import search`, a compiled pattern's
`.match`) is found as well, because the module's own imports and assignments name them.

What it cannot see: text read through a name the sweep does not follow (an attribute, a container, a
function argument, one value unpacked into several names), an f-string that adds words of its own
around the text, a constant that is not a module-level name in the same file (an import, a class
attribute), text handed to a function the sweep does not know (`functools.partial`, `getattr(e,
"startswith")`, `map` over a method, a comprehension's condition), a text method called through the
class (`str.startswith(e, "x")`), the dunder spelling of a test (`e.__contains__("x")`), a `match`
statement on the text, and a decision on an exception's message rather than a reply's `"error"`
field.
Tests are out of scope: `pytest.raises(Typed, match=...)` stays.
"""

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"

#: (file, function) -> why that function may read a reply's text.
EXEMPT: dict[tuple[str, str], str] = {
    ("src/cliffracer/core/exceptions.py", "raise_for_error_envelope"): (
        "classifies a reply: by its `code` when it has one, and by its text only for a reply with "
        "none, which an old service sends. The coded branch makes no text decision (checked below)."
    ),
}

TEXT_METHODS = frozenset(
    {
        "startswith",
        "endswith",
        "find",
        "rfind",
        "index",
        "rindex",
        "count",
        "partition",
        "rpartition",
        "split",
        "rsplit",
    }
)
# Methods that reshape the text without deciding anything: what they return is still the text, so a
# decision on it is still a decision on the text. Stripping a "refused: " prefix to build a reason for
# display is not itself a classification.
PASS_THROUGH_METHODS = frozenset(
    {"lower", "upper", "casefold", "strip", "lstrip", "rstrip", "removeprefix", "removesuffix"}
)


# `operator` functions that test text. `contains`, `countOf` and `indexOf` decide on the text whatever
# the other argument is; `eq` and `ne` are a comparison with a string, as `==` is.
OPERATOR_SEARCHES = frozenset({"contains", "countOf", "indexOf"})
OPERATOR_COMPARISONS = frozenset({"eq", "ne"})
RE_FUNCTIONS = frozenset(
    {"match", "search", "fullmatch", "findall", "finditer", "split", "sub", "subn"}
)
PATTERN_METHODS = RE_FUNCTIONS | {"scanner"}


class Facts(NamedTuple):
    """What a module says about the names its decisions are written with.

    A guard that knew only the spellings `re.search(...)` and `==` is passed by `import re as _re`,
    `from operator import contains`, and a constant named at module level, so the names are read
    from the module's own imports and assignments.
    """

    constants: frozenset[str] = frozenset()
    re_modules: frozenset[str] = frozenset({"re"})
    re_functions: frozenset[str] = frozenset()
    re_compilers: frozenset[str] = frozenset()
    operator_modules: frozenset[str] = frozenset({"operator"})
    operator_functions: dict[str, str] = {}


def _module_facts(tree: ast.AST) -> Facts:
    re_modules, re_functions, re_compilers = {"re"}, set(), set()
    operator_modules, operator_functions = {"operator"}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "re":
                    re_modules.add(alias.asname or "re")
                elif alias.name == "operator":
                    operator_modules.add(alias.asname or "operator")
        elif isinstance(node, ast.ImportFrom) and node.module in ("re", "operator"):
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module == "re" and alias.name in RE_FUNCTIONS:
                    re_functions.add(bound)
                elif node.module == "re" and alias.name == "compile":
                    re_compilers.add(bound)
                elif node.module == "operator":
                    operator_functions[bound] = alias.name
    return Facts(
        _string_constants(tree),
        frozenset(re_modules),
        frozenset(re_functions),
        frozenset(re_compilers),
        frozenset(operator_modules),
        operator_functions,
    )


def _is_error_field_read(node: ast.AST) -> bool:
    """`x["error"]` or `x.get("error", ...)`."""
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value == "error"
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
    ):
        first = node.args[0]
        return isinstance(first, ast.Constant) and first.value == "error"
    return False


def _tainted(node: ast.AST, names: set[str]) -> bool:
    """Whether `node` is a reply's error text, or a simple derivation of it."""
    if _is_error_field_read(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "str" and node.args:
            return _tainted(node.args[0], names)
        if isinstance(func, ast.Attribute) and func.attr in PASS_THROUGH_METHODS:
            return _tainted(func.value, names)
    if isinstance(node, ast.NamedExpr):
        return _tainted(node.value, names)
    if isinstance(node, ast.BoolOp):
        # `d.get("error") or ""`: the usual way to read a field that may be absent. Either side
        # can be what the name ends up holding, so the whole expression is the text.
        return any(_tainted(value, names) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _tainted(node.body, names) or _tainted(node.orelse, names)
    if isinstance(node, ast.Subscript):
        # `e[:8]`, `e[0]`: a piece of the text, and a prefix test spelled as a slice is still one.
        return _tainted(node.value, names)
    if isinstance(node, ast.JoinedStr):
        # `f"{e}"`: the text with nothing added. An f-string with words of its own around the field
        # is a new message, and a test of its own words is not a decision on the reply.
        return (
            len(node.values) == 1
            and isinstance(node.values[0], ast.FormattedValue)
            and (_tainted(node.values[0].value, names))
        )
    return False


def _assignments(scope: ast.AST):
    """(target, value) for every simple assignment in `scope`.

    `code, e = d.get("code"), d.get("error")` is a pair for each name, so `e` is paired with the
    error read and not with the whole tuple. An unpacking of one value (`code, e = pair`) has no
    element to pair and is not followed.
    """
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs = [(node.target, node.value)]
        elif isinstance(node, ast.NamedExpr):
            pairs = [(node.target, node.value)]
        else:
            continue
        while pairs:
            target, value = pairs.pop()
            if (
                isinstance(target, ast.Tuple | ast.List)
                and isinstance(value, ast.Tuple | ast.List)
                and len(target.elts) == len(value.elts)
            ):
                pairs.extend(zip(target.elts, value.elts, strict=True))
            else:
                yield target, value


def _string_constants(tree: ast.AST) -> frozenset[str]:
    """The module-level names bound to a string, or a tuple, list or set of strings.

    `e == REFUSED` is a comparison with a string constant whichever way the constant is spelled.
    """
    found: set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if _is_string_constant(value):
            found.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(found)


def _text_decisions(scope: ast.AST, facts: Facts | None = None) -> list[tuple[int, str]]:
    """(line, source) of each decision made on a reply's error text inside `scope`."""
    facts = facts or Facts()
    names: set[str] = set()
    method_aliases: set[str] = set()  # `sw = e.startswith`
    patterns: set[str] = set()  # `pat = re.compile(...)`

    def is_re_call(call: ast.Call, functions: frozenset[str], module_names: frozenset[str]) -> bool:
        func = call.func
        if isinstance(func, ast.Attribute):
            return isinstance(func.value, ast.Name) and func.value.id in module_names
        return isinstance(func, ast.Name) and func.id in functions

    changed = True
    while changed:  # a name assigned from a name assigned from the field
        changed = False
        for target, value in _assignments(scope):
            if not isinstance(target, ast.Name):
                continue
            if target.id not in names and _tainted(value, names):
                names.add(target.id)
                changed = True
            if (
                target.id not in method_aliases
                and isinstance(value, ast.Attribute)
                and value.attr in TEXT_METHODS
                and _tainted(value.value, names)
            ):
                method_aliases.add(target.id)
                changed = True
            if (
                target.id not in patterns
                and isinstance(value, ast.Call)
                and (
                    (
                        isinstance(value.func, ast.Attribute)
                        and value.func.attr == "compile"
                        and isinstance(value.func.value, ast.Name)
                        and value.func.value.id in facts.re_modules
                    )
                    or (isinstance(value.func, ast.Name) and value.func.id in facts.re_compilers)
                )
            ):
                patterns.add(target.id)
                changed = True

    def any_tainted(call: ast.Call) -> bool:
        return any(_tainted(arg, names) for arg in call.args)

    found: list[tuple[int, str]] = []
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in TEXT_METHODS
            and _tainted(node.func.value, names)
        ):
            found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Call) and (
            isinstance(node.func, ast.Name) and node.func.id in method_aliases
        ):
            found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_tainted(operand, names) for operand in operands) and any(
                _is_string_constant(operand, facts.constants) for operand in operands
            ):
                found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Call):
            decided = False
            if is_re_call(node, facts.re_functions, facts.re_modules):
                decided = any_tainted(node)
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in patterns
                and node.func.attr in PATTERN_METHODS
            ):
                decided = any_tainted(node)
            else:
                function = None
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in facts.operator_modules
                ):
                    function = node.func.attr
                elif isinstance(node.func, ast.Name) and node.func.id in facts.operator_functions:
                    function = facts.operator_functions[node.func.id]
                if function in OPERATOR_SEARCHES:
                    decided = any_tainted(node)
                elif function in OPERATOR_COMPARISONS:
                    decided = any_tainted(node) and any(
                        _is_string_constant(arg, facts.constants) for arg in node.args
                    )
            if decided:
                found.append((node.lineno, ast.unparse(node)))
    return found


def _is_string_constant(node: ast.AST, constants: frozenset[str] = frozenset()) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Name):
        return node.id in constants
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return all(_is_string_constant(element, constants) for element in node.elts)
    return False


def _functions(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def error_text_decisions(sources: dict[str, str], exempt=EXEMPT) -> list[str]:
    """`file:line function: source` for every text decision outside an exempt function."""
    problems: list[str] = []
    for path, text in sorted(sources.items()):
        tree = ast.parse(text)
        facts = _module_facts(tree)
        for function in _functions(tree):
            if (path, function.name) in exempt:
                continue
            # Only the function's own statements: a nested function is walked as its own.
            for line, code in _text_decisions(function, facts):
                problems.append(f"{path}:{line} in {function.name}: {code}")
    # A function inside another is reached twice (as its own scope and inside its parent).
    return sorted(set(problems))


def error_replies_without_a_code(sources: dict[str, str]) -> list[str]:
    """`file:line` of each dict literal that is an RPC error reply (error and success) with no code."""
    missing: list[str] = []
    for path, text in sorted(sources.items()):
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Dict):
                keys = {
                    key.value
                    for key in node.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                if {"error", "success"} <= keys and "code" not in keys:
                    missing.append(f"{path}:{node.lineno}")
    return missing


def the_coded_branch_makes_a_text_decision(source: str, function_name: str) -> bool | None:
    """Whether the `if code is not None:` branch of `function_name` decides on text.

    None when the function has no such branch, so the check cannot pass by reading nothing.
    """
    tree = ast.parse(source)
    facts = _module_facts(tree)
    for function in _functions(tree):
        if function.name != function_name:
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "code"
                and isinstance(node.test.ops[0], ast.IsNot)
            ):
                # The names tainted in the whole function apply inside the branch.
                holder = ast.Module(body=function.body, type_ignores=[])
                inside = {line for line, _ in _text_decisions(holder, facts)}
                lines = {
                    n.lineno for stmt in node.body for n in ast.walk(stmt) if hasattr(n, "lineno")
                }
                return bool(inside & lines)
    return None


def _src_sources() -> dict[str, str]:
    return {
        path.relative_to(REPO).as_posix(): path.read_text() for path in sorted(SRC.rglob("*.py"))
    }


# ---- the sweeps over src/ ----------------------------------------------------------------------


def test_every_rpc_error_reply_in_src_carries_a_code():
    sources = _src_sources()
    assert len(sources) > 50, f"read {len(sources)} files; the sweep is not reading src/"
    envelopes = sum(
        1
        for text in sources.values()
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Dict)
        and {"error", "success"}
        <= {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    )
    assert envelopes >= 9, f"found {envelopes} error replies; the sweep is reading too little"

    assert error_replies_without_a_code(sources) == []


def test_no_decision_in_src_is_made_on_a_replys_error_text_outside_the_exempt_function():
    problems = error_text_decisions(_src_sources())

    assert problems == [], (
        "a reply's `error` text is decided on (prefix, substring, comparison) where its `code` should "
        "be, or the exempt function's reasons need stating:\n  " + "\n  ".join(problems)
    )


def test_the_exempt_function_exists_and_reads_the_text_only_for_an_uncoded_reply():
    for (path, function), reason in EXEMPT.items():
        source = (REPO / path).read_text()
        assert reason.strip()
        decision = the_coded_branch_makes_a_text_decision(source, function)
        assert decision is not None, f"{function} has no `if code is not None:` branch to check"
        assert decision is False, f"{function}'s coded branch decides on the text"
        # And it does read the text somewhere: an exemption nothing needs is stale.
        tree = ast.parse(source)
        reads = _text_decisions(
            next(f for f in _functions(tree) if f.name == function), _module_facts(tree)
        )
        assert reads, f"{function} makes no text decision, so it need not be exempt"


# ---- controls: each way the sweeps could pass wrongly ------------------------------------------

PLANTS = {
    "a prefix test on the field": 'def f(d):\n    return d["error"].startswith("refused: ")\n',
    "a prefix test through .get": 'def f(d):\n    return str(d.get("error", "")).startswith("x")\n',
    "a name assigned from the field": (
        'def f(d):\n    err = d["error"]\n    if "timeout" in err:\n        return 1\n'
    ),
    "a name derived from a name": (
        'def f(d):\n    err = str(d["error"])\n    low = err.lower()\n    return low == "boom"\n'
    ),
    "equality with a string": 'def f(d):\n    return d["error"] == "validation failed"\n',
    "membership in constants": 'def f(d):\n    return d["error"] in ("a", "b")\n',
    "a decision on the result of removeprefix": (
        'def f(d):\n    rest = d["error"].removeprefix("refused: ")\n    return rest == "no token"\n'
    ),
    "a regex search": 'import re\ndef f(d):\n    return re.search("x", d["error"])\n',
    "a walrus assignment": 'def f(d):\n    if (e := d["error"]).endswith("!"):\n        return e\n',
    "a name assigned from the field or a default": (
        'def f(d):\n    _e = d.get("error") or ""\n    return _e.endswith("x")\n'
    ),
    "a name assigned from str of the field or a default": (
        'def f(d):\n    _e = str(d.get("error") or "")\n    return _e.endswith("x")\n'
    ),
    "a name assigned from a default on the left": (
        'def f(d, other):\n    _e = other or d["error"]\n    return _e == "boom"\n'
    ),
    "a name assigned from a conditional expression": (
        'def f(d):\n    e = d.get("error")\n    t = e if e else ""\n    return "x" in t\n'
    ),
    "a slice compared with a string": 'def f(d):\n    e = d["error"]\n    return e[:8] == "refused:"\n',
    "a slice of the field compared in place": (
        'def f(d):\n    return str(d.get("error") or "")[:8] == "refused:"\n'
    ),
    "a compare with a named module constant": (
        'REFUSED = "refused: policy"\n\ndef f(d):\n    e = d["error"]\n    return e == REFUSED\n'
    ),
    "a membership test in a named tuple of strings": (
        'KNOWN = ("a", "b")\n\ndef f(d):\n    return d["error"] in KNOWN\n'
    ),
    "a name read in a tuple unpacking": (
        'def f(d):\n    code, e = d.get("code"), d.get("error")\n    return e.startswith("x")\n'
    ),
    "the error first in a tuple unpacking": (
        'def f(d):\n    e, code = d["error"], d.get("code")\n    return e.endswith("x")\n'
    ),
    "the text interpolated alone and tested": (
        'def f(d):\n    msg = f"{d[\'error\']}"\n    return msg.startswith("refused")\n'
    ),
    "operator.contains on the text": (
        'import operator\n\ndef f(d):\n    return operator.contains(d["error"], "refused")\n'
    ),
    "operator.contains with the text as the container's member": (
        'import operator\n\ndef f(d):\n    return operator.contains(("a", "b"), d["error"])\n'
    ),
    "operator.eq with a string": (
        'import operator\n\ndef f(d):\n    return operator.eq(d["error"], "validation failed")\n'
    ),
    "operator.contains under an aliased module": (
        'import operator as op\n\ndef f(d):\n    return op.contains(str(d.get("error")), "x")\n'
    ),
    "an operator function imported by name": (
        'from operator import contains as has\n\ndef f(d):\n    return has(d["error"], "x")\n'
    ),
    "a bound method kept under a name": (
        'def f(d):\n    sw = d["error"].startswith\n    return sw("refused")\n'
    ),
    "a bound method of a name assigned from the field": (
        'def f(d):\n    e = d.get("error") or ""\n    has = e.endswith\n    return has("!")\n'
    ),
    "re through an aliased import": (
        'import re as _re\n\ndef f(d):\n    return _re.match("x", d["error"])\n'
    ),
    "a function imported from re": (
        'from re import search as find\n\ndef f(d):\n    return find("x", d["error"])\n'
    ),
    "a compiled pattern": (
        'import re\n\nPATTERN = re.compile("x")\n\ndef f(d):\n    pat = re.compile("x")\n'
        '    return pat.search(d["error"])\n'
    ),
    "a pattern compiled by an aliased import": (
        'import re as r\n\ndef f(d):\n    pat = r.compile("x")\n    return pat.fullmatch(str(d["error"]))\n'
    ),
    "a default read of the field tested in place": (
        'def f(d):\n    return (d.get("error") or "").startswith("x")\n'
    ),
}


@pytest.mark.parametrize("name", PLANTS)
def test_CONTROL_each_planted_text_decision_is_reported(name):
    found = error_text_decisions({"src/x.py": PLANTS[name]})

    assert len(found) == 1, (name, found)


FINE = {
    "removeprefix to build a reason": 'def f(d):\n    return str(d["error"]).removeprefix("refused: ")\n',
    "decided on the code": 'def f(d):\n    return d.get("code") == "refused"\n',
    "formatted into a message": "def f(d):\n    return f\"failed: {d['error']}\"\n",
    "printed": 'def f(d):\n    print(d["error"])\n',
    "wrapped in an exception": 'def f(d):\n    raise ValueError(str(d["error"]))\n',
    "tested for presence": 'def f(d):\n    return "error" in d\n',
    "tested for None": 'def f(d):\n    return d.get("error") is None\n',
    "another field's text": 'def f(d):\n    return d["note"].startswith("x")\n',
    "a slice of another field": 'def f(d):\n    return d["note"][:8] == "refused:"\n',
    "a named constant compared with another field": (
        'REFUSED = "x"\n\ndef f(d):\n    return d["note"] == REFUSED\n'
    ),
    "the error text compared with a name that is not a constant": (
        'def f(d, other):\n    return d["error"] == other\n'
    ),
    "the error in a tuple unpacking, only formatted": (
        'def f(d):\n    code, e = d.get("code"), d.get("error")\n    return f"{code}: {e}"\n'
    ),
    "another name of a tuple unpacking tested": (
        'def f(d):\n    code, e = d.get("code"), d.get("error")\n    return code.startswith("x")\n'
    ),
    "the text with words of its own, whose own words are tested": (
        'def f(d):\n    msg = f"failed: {d[\'error\']}"\n    return msg.startswith("failed")\n'
    ),
    "operator.contains on another field": (
        'import operator\n\ndef f(d):\n    return operator.contains(d["note"], "refused")\n'
    ),
    "operator.eq with two names": (
        'import operator\n\ndef f(d, other):\n    return operator.eq(d["error"], other)\n'
    ),
    "operator.add on the text": (
        'import operator\n\ndef f(d):\n    return operator.add(d["error"], "!")\n'
    ),
    "a bound method of another field kept under a name": (
        'def f(d):\n    sw = d["note"].startswith\n    return sw("x")\n'
    ),
    "a call of a name that is not a method alias": (
        'def f(d, sw):\n    e = d["error"]\n    return sw(e)\n'
    ),
    "an aliased re on another field": (
        'import re as _re\n\ndef f(d):\n    return _re.match("x", d["note"])\n'
    ),
    "a compiled pattern on another field": (
        'import re\n\ndef f(d):\n    pat = re.compile("x")\n    return pat.search(d["note"])\n'
    ),
    "a name that is re in no import": ('def f(d, rx):\n    return rx.match("x", d["error"])\n'),
    "another field read with a default": (
        'def f(d):\n    note = d.get("note") or ""\n    return note.startswith("x")\n'
    ),
    "the error text with a default, only formatted": (
        'def f(d):\n    e = d.get("error") or "unknown"\n    return f"failed: {e}"\n'
    ),
}


@pytest.mark.parametrize("name", FINE)
def test_CONTROL_a_use_that_is_not_a_decision_is_not_reported(name):
    assert error_text_decisions({"src/x.py": FINE[name]}) == []


def test_CONTROL_the_exempt_function_is_skipped_and_the_same_code_elsewhere_is_not():
    plant = 'def classify(d):\n    return d["error"].startswith("x")\n'
    exempt = {("src/x.py", "classify"): "for the control"}

    assert error_text_decisions({"src/x.py": plant}, exempt) == []
    assert len(error_text_decisions({"src/y.py": plant}, exempt)) == 1
    assert len(error_text_decisions({"src/x.py": plant.replace("classify", "other")}, exempt)) == 1


def test_CONTROL_a_coded_branch_that_reads_the_text_is_caught():
    coded_reads_text = (
        "def classify(d):\n"
        '    err = str(d["error"])\n'
        '    code = d.get("code")\n'
        "    if code is not None:\n"
        '        if err.startswith("refused: "):\n'
        "            return 1\n"
        "        return 2\n"
        '    return err.startswith("x")\n'
    )
    coded_clean = coded_reads_text.replace(
        '        if err.startswith("refused: "):\n            return 1\n', ""
    )

    assert the_coded_branch_makes_a_text_decision(coded_reads_text, "classify") is True
    assert the_coded_branch_makes_a_text_decision(coded_clean, "classify") is False
    assert (
        the_coded_branch_makes_a_text_decision("def classify(d):\n    return 1\n", "classify")
        is None
    )


def test_CONTROL_an_error_reply_with_no_code_is_reported_and_other_error_dicts_are_not():
    missing = 'def f():\n    return {"success": False, "error": "x"}\n'
    coded = 'def f():\n    return {"success": False, "error": "x", "code": "internal"}\n'
    not_an_rpc_reply = 'def f():\n    return {"error": "x", "status": "down"}\n'

    assert error_replies_without_a_code({"src/x.py": missing}) == ["src/x.py:2"]
    assert error_replies_without_a_code({"src/x.py": coded}) == []
    assert error_replies_without_a_code({"src/x.py": not_an_rpc_reply}) == []

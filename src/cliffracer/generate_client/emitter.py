"""Description -> Python source for a typed client. Deterministic.

Output is deterministic: the same Description produces identical bytes across
environments without timestamps or system metadata.

The emitted code is formatted by construction to satisfy standard ruff formatting
constraints across environments without requiring external formatter invocation.

Key formatting conventions: strings are double-quoted; calls use trailing commas
for stable multiline formatting; and `from __future__ import annotations` is omitted
so `inspect.signature` returns resolved class objects.
"""

from __future__ import annotations

import enum
import functools
import keyword
import math
import textwrap
import unicodedata
from dataclasses import dataclass
from typing import Any

from cliffracer.core.typed_rpc import reserved_rpc_method_names, shown, unimportable_models
from cliffracer.introspect import Description, Method

_SCALAR_TEXT = {"str": "str", "int": "int", "float": "float", "bool": "bool", "none": "None"}


class CannotEmit(Exception):
    """The Description names something the generator refuses to express."""


def _string_literal(value: str) -> str:
    """A string literal spelled the way ruff format spells one, reading back as
    the value it was built from.

    The escaping is `ascii`'s, which is Python's own. `json.dumps` is not safe
    here: it escapes a non-BMP character as a UTF-16 surrogate pair, which is
    valid JSON and, read back as Python, two lone surrogates -- so an emoji in a
    default came out of the generated client as a different string than went in.

    The quote is chosen the way ruff chooses: double, unless single STRICTLY
    reduces escapes. A tie -- one of each -- stays double. Measured against
    `ruff format` for 28 strings, listed in
    `tests/unit/test_generate_client.py::AWKWARD_STRINGS`, each asserted three
    ways: the spelling ruff leaves alone, the value it reads back as, and that
    it is pure ASCII so the file needs no encoding declaration.
    """
    raw = ascii(value)
    source_quote, inner = raw[0], raw[1:-1]
    want = "'" if value.count('"') > value.count("'") else '"'
    if source_quote == want:
        return raw
    # Re-quote: what needed escaping for the old quote no longer does, and the
    # new one now does. Walked as escape PAIRS, because a literal backslash is
    # `\\` here and a naive replace of `\"` would cut into one.
    out: list[str] = []
    i = 0
    while i < len(inner):
        if inner[i] == "\\":
            pair = inner[i : i + 2]
            out.append(source_quote if pair == "\\" + source_quote else pair)
            i += 2
        elif inner[i] == want:
            out.append("\\" + want)
            i += 1
        else:
            out.append(inner[i])
            i += 1
    return want + "".join(out) + want


def _in_key_order(mapping: dict[Any, Any]) -> list[tuple[Any, Any]]:
    """The items of a default's dict, by key.

    A description reaches the generator in two ways, in process from the class and as the bytes of
    `{service}.describe`, which are written with sorted keys. The class keeps a dict in the order
    it was declared in, so the same default came out in two orders and `--check`, which compares
    bytes, called a client stale depending on which way it was generated. Both now write it sorted.
    """
    return sorted(mapping.items(), key=lambda item: str(item[0]))


def _literal(value: Any) -> str:
    """A Python literal for a JSON-shaped value, formatted the way ruff wants.

    Booleans and None go through `repr`, because `json.dumps` would spell them
    `true` and `null`.

    A non-finite float is the one case where `repr` does not produce something
    Python can read back: it gives `inf`, `-inf` and `nan`, which are names
    rather than literals, so the generated module raised `NameError` at import.
    `float("inf")` is an expression rather than a literal, which is the price of
    spelling a value the language has no literal for, and it needs no import.
    """
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, str):
        return _string_literal(value)
    if isinstance(value, float) and not math.isfinite(value):
        # `str` spells these `inf`, `-inf` and `nan` -- exactly the argument
        # `float()` takes back.
        return f'float("{value}")'
    # Containers are rendered MEMBER BY MEMBER through this same function, not
    # through `repr`. `repr` recurses with its own rules, so a `list[float]`
    # holding an infinity came out as `[inf]` -- the same NameError one level
    # down -- and a `list[str]` came out as `['a']`, which ruff reformats. A
    # parameterised container is a supported annotation, so both are reachable:
    # it is the BARE `list` that `describe` refuses.
    if isinstance(value, list):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    if isinstance(value, tuple):
        # A one-tuple needs its trailing comma or it is just a parenthesised
        # value. `dump_python(mode="json")` turns tuples into lists, so this is
        # for a default that reaches the emitter by another route.
        inner = ", ".join(_literal(v) for v in value)
        return f"({inner},)" if len(value) == 1 else f"({inner})"
    if isinstance(value, dict):
        items = ", ".join(f"{_literal(k)}: {_literal(v)}" for k, v in _in_key_order(value))
        return "{" + items + "}"
    return repr(value)


def _docstring(doc: str) -> str:
    """A one-line docstring literal. Triple-quoted, because that is what a
    reader expects and what every style guide in reach asks for; a doc that
    could break out of the quotes falls back to a plain double-quoted string,
    which is still a docstring and is still safe."""
    if '"""' in doc or doc.endswith('"') or "\\" in doc or "\n" in doc:
        return _literal(doc)
    return f'"""{doc}"""'


def _docstring_lines(doc: str, indent: str) -> list[str]:
    """A docstring literal, wrapped so that no line passes `LINE_LENGTH`.

    A docstring is the one thing in the emitted file whose width the formatter
    will not fix. `ruff format` does not reflow the contents of a string, so an
    over-long docstring passes `ruff format --check` and only `E501` sees it --
    which is why the guard that measures formatting could not catch this and
    the one that measures line length had no long doc to measure. `_assignment`
    and `_signature` both wrap for the same target; this did not, and one
    ordinary 130-character sentence emitted a 144-character line.

    Wrapping the doc's own line breaks is not enough on its own: the sentence
    above has none. Neither is truncating to the first line, which is that
    whole sentence. The text is re-wrapped to the width actually available.

    The closing quotes go on their own line, which is also what makes a doc
    ending in `"` safe here: ruff rewrites a single-content-line docstring that
    ends in a quote, but leaves a genuinely multi-line one alone.
    """
    single = _docstring(doc)
    if "\n" not in doc and len(indent) + len(single) <= LINE_LENGTH:
        return [indent + single]

    width = LINE_LENGTH - len(indent)
    body: list[str] = []
    for paragraph in _escape_docstring_text(doc).split("\n"):
        if not paragraph.strip():
            body.append("")
            continue
        # Three columns held back on the very first line for the opening
        # quotes.
        #
        # A single token longer than the width -- a URL, a dotted name -- is
        # left whole and over the limit rather than cut in half, because that
        # is what the target tolerates: measured, `E501` does not flag a line
        # whose overlong part carries no whitespace, while it does flag a
        # 159-character line of ordinary words. Breaking the token would trade
        # a line the linter accepts for a URL that no longer resolves.
        held_back = "   " if not body else ""
        body.extend(
            textwrap.wrap(
                paragraph,
                width=width,
                initial_indent=held_back,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [""]
        )
    body[0] = '"""' + body[0][3:]
    while body and not body[-1].strip():
        body.pop()
    if len(body) == 1 and "\n" not in doc:
        # One content line, which is the one shape `ruff format` rewrites: it joins the closing
        # quotes back on whatever the length, so text of 75 to 77 characters was a file that
        # needs formatting. Joined it is over the width, which E501 reports. Two lines are
        # neither: the text is wrapped at about half the width, so the docstring has a second
        # line and ruff leaves its closing quotes alone. A text with no space has nowhere to
        # break, and E501 does not report a line whose overlong part has none.
        text = _escape_docstring_text(doc)
        if " " not in text.strip():
            return [indent + single]
        halves = textwrap.wrap(text, width=len(text) // 2 + 8, break_long_words=False)
        body = ['"""' + halves[0], *halves[1:]]
    return [indent + line if line else "" for line in body] + [indent + '"""']


def _module_prefix(module: str) -> str:
    return "".join(p.capitalize() for p in module.replace(".", "_").split("_"))


def _natural_compare(a: str, b: str) -> int:
    """Compare as ruff's import sorter does: digit runs by value, not by character.

    The `natord` ordering ruff's isort uses, so `v2` sorts before `v10`. A run
    starting with `0` compares left-aligned, as a fraction would, so `v02`
    sorts before `v2` and `v05` before `v4`; any other run compares by length
    and then digit by digit.
    """
    i = j = 0
    while True:
        ca = a[i] if i < len(a) else None
        cb = b[j] if j < len(b) else None
        if ca is not None and cb is not None and ca.isdigit() and cb.isdigit():
            if ca == "0" or cb == "0":
                result = _compare_digits_left(a[i:], b[j:])
            else:
                result = _compare_digits_right(a[i:], b[j:])
            if result:
                return result
        if ca is None and cb is None:
            return 0
        if ca is None:
            return -1
        if cb is None:
            return 1
        if ca != cb:
            return -1 if ca < cb else 1
        i += 1
        j += 1


def _compare_digits_right(a: str, b: str) -> int:
    """Two digit runs without leading zeros: the longer is larger, else the first difference."""
    bias = 0
    for k in range(max(len(a), len(b)) + 1):
        da = k < len(a) and a[k].isdigit()
        db = k < len(b) and b[k].isdigit()
        if not da and not db:
            return bias
        if not da:
            return -1
        if not db:
            return 1
        if not bias and a[k] != b[k]:
            bias = -1 if a[k] < b[k] else 1
    return bias


def _compare_digits_left(a: str, b: str) -> int:
    """Two digit runs, one with a leading zero: compared digit by digit from the left."""
    for k in range(max(len(a), len(b)) + 1):
        da = k < len(a) and a[k].isdigit()
        db = k < len(b) and b[k].isdigit()
        if not da and not db:
            return 0
        if not da:
            return -1
        if not db:
            return 1
        if a[k] != b[k]:
            return -1 if a[k] < b[k] else 1
    return 0


def _module_order(module: str) -> tuple[Any, Any]:
    """Ruff's isort key for a module: natural and case-insensitive, then natural."""
    return (_NaturalKey(module.lower()), _NaturalKey(module))


def _member_order(name: str) -> tuple[int, Any, Any]:
    """Ruff's isort key for an imported name: constants, then classes, then the
    rest, each natural and case-insensitive, then natural."""
    if len(name) > 1 and any(c.isupper() for c in name) and not any(c.islower() for c in name):
        kind = 0
    elif name[:1].isupper():
        kind = 1
    else:
        kind = 2
    return (kind, _NaturalKey(name.lower()), _NaturalKey(name))


@functools.total_ordering
class _NaturalKey:
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _NaturalKey) and _natural_compare(self.text, other.text) == 0

    def __lt__(self, other: _NaturalKey) -> bool:
        return _natural_compare(self.text, other.text) < 0

    def __hash__(self) -> int:
        return hash(self.text)


def _escape_docstring_text(text: str) -> str:
    """Escape backslashes and triple quotes so the text cannot break out of a triple-quoted docstring."""
    return text.replace("\\", "\\\\").replace('"""', r"\"\"\"")


def _split_qualname(qualname: str) -> tuple[str, str]:
    """A qualname's importable head and the attribute tail that follows it.

    `Outer.Inner` -> `("Outer", ".Inner")`, `Order` -> `("Order", "")`, so the
    tail concatenates onto whatever name the head is bound to without the
    caller testing for a dot.
    """
    head, dot, tail = qualname.partition(".")
    return head, (dot + tail) if dot else ""


# AN ANNOTATION IS A TREE UNTIL IT IS LAID OUT, because ruff formats one that
# is too long for its line by splitting it at its brackets and before `| None`,
# and a string can only be pasted whole. Four kinds cover everything
# `annotation_text` writes.


@dataclass(frozen=True)
class _Name:
    """A name or a spelling that is never split: `str`, `MNOrder.Line`, `Literal[()]`."""

    text: str


@dataclass(frozen=True)
class _Subscript:
    """`head[arg, ...]`: `list[X]` and `dict[str, X]`."""

    head: str
    args: tuple[_Annotation, ...]


@dataclass(frozen=True)
class _Optional:
    """`X | None`."""

    inner: _Annotation


@dataclass(frozen=True)
class _Literal:
    """`Literal[v, ...]`, each value already spelled as a literal."""

    values: tuple[str, ...]


_Annotation = _Name | _Subscript | _Optional | _Literal


def _annotation(ref: dict[str, Any], aliases: dict[tuple[str, str], str] | None) -> _Annotation:
    kind = ref["kind"]
    if kind == "scalar":
        if ref["name"] not in _SCALAR_TEXT:
            raise CannotEmit(f"unknown scalar type {ref['name']!r}")
        return _Name(_SCALAR_TEXT[ref["name"]])
    if kind == "model":
        # A qualname can be dotted -- `Outer.Inner` for a nested model -- and
        # only its HEAD is ever imported or aliased, by `_models` and the import
        # loop in `emit`. So the lookup is on the head and the tail is
        # re-attached to whatever that head is bound to. Looking up the whole
        # qualname always missed for a nested model and fell through to the
        # fallback, which invented a name no import binds: the generated file
        # raised NameError on import.
        module, head, tail = ref["module"], *_split_qualname(ref["qualname"])
        if aliases and (module, head) in aliases:
            return _Name(aliases[(module, head)] + tail)
        # Unreachable from `emit`, which always passes aliases covering every
        # model in the description. It is the answer for a direct caller of this
        # public function, and it spells the name `emit` WOULD bind on a
        # collision -- one prefix helper, so the two cannot drift. They had:
        # this computed the prefix from the last module component alone while
        # `emit` used all of them, so the same model had two spellings.
        return _Name(f"{_module_prefix(module)}{head}{tail}")
    if kind == "list":
        return _Subscript("list", (_annotation(ref["item"], aliases),))
    if kind == "dict":
        return _Subscript("dict", (_Name("str"), _annotation(ref["value"], aliases)))
    if kind == "optional":
        return _Optional(_annotation(ref["inner"], aliases))
    if kind == "stream":
        return _Subscript("_typing.AsyncIterator", (_annotation(ref["item"], aliases),))
    if kind == "literal":
        if not ref["values"]:
            return _Name("Literal[()]")
        return _Literal(tuple(_literal(v) for v in ref["values"]))
    raise CannotEmit(f"unknown TypeRef kind {kind!r}")


def _flat(node: _Annotation) -> str:
    if isinstance(node, _Name):
        return node.text
    if isinstance(node, _Subscript):
        return f"{node.head}[{', '.join(_flat(a) for a in node.args)}]"
    if isinstance(node, _Optional):
        return f"{_flat(node.inner)} | None"
    return f"Literal[{', '.join(node.values)}]"


def _names_in(node: _Annotation) -> set[str]:
    """The names an annotation reads when it is evaluated: its heads, `str` and each model."""
    if isinstance(node, _Name):
        return {node.text.partition(".")[0]}
    if isinstance(node, _Subscript):
        return {node.head}.union(*(_names_in(arg) for arg in node.args))
    if isinstance(node, _Optional):
        return _names_in(node.inner)
    return {"Literal"}


def _unshadowed(node: _Annotation, spelling: dict[str, str]) -> _Annotation:
    """`node` with each name in `spelling` written as the module-level alias that stands for it."""
    if not spelling:
        return node
    if isinstance(node, _Name):
        head, dot, rest = node.text.partition(".")
        return _Name(spelling.get(head, head) + dot + rest)
    if isinstance(node, _Subscript):
        return _Subscript(
            spelling.get(node.head, node.head), tuple(_unshadowed(a, spelling) for a in node.args)
        )
    if isinstance(node, _Optional):
        return _Optional(_unshadowed(node.inner, spelling))
    return node


def annotation_text(ref: dict[str, Any], aliases: dict[tuple[str, str], str] | None = None) -> str:
    return _flat(_annotation(ref, aliases))


def _laid_out(
    node: _Annotation,
    indent: str,
    prefix: str,
    suffix: str,
    *,
    statement: bool = False,
    comment: str = "",
) -> list[str]:
    """`prefix` + the annotation + `suffix`, split the way ruff splits it when too long.

    The flat form when it fits. Otherwise a subscript with several elements
    puts one per line, each with a trailing comma: ruff keeps that shape
    whatever the width, so no choice of its own has to be copied. A single
    element is hugged by its brackets with no comma, as ruff writes it. `X |
    None` breaks before `| None`, and is parenthesised where it is a whole
    statement's right-hand side or a return annotation, where ruff writes the
    parentheses and the grammar needs them.

    `comment` goes after `suffix` on the last line and is not measured: ruff
    decides whether code fits without its trailing comment.
    """
    flat = f"{indent}{prefix}{_flat(node)}{suffix}"
    if len(flat) <= LINE_LENGTH:
        return [flat + comment]
    lines = _split(node, indent, prefix, suffix, statement)
    lines[-1] += comment
    return lines


def _split(node: _Annotation, indent: str, prefix: str, suffix: str, statement: bool) -> list[str]:
    flat = f"{indent}{prefix}{_flat(node)}{suffix}"
    inner = indent + "    "
    if statement and isinstance(node, _Optional | _Name):
        return [f"{indent}{prefix}(", *_laid_out(node, inner, "", ""), f"{indent}){suffix}"]
    if isinstance(node, _Optional):
        return [*_laid_out(node.inner, indent, prefix, ""), f"{indent}| None{suffix}"]
    if isinstance(node, _Name):
        return [flat]
    head, elements = (
        (node.head, list(node.args))
        if isinstance(node, _Subscript)
        else ("Literal", [_Name(v) for v in node.values])
    )
    lines = [f"{indent}{prefix}{head}["]
    if len(elements) == 1:
        lines += _laid_out(elements[0], inner, "", "")
    else:
        for element in elements:
            lines += _laid_out(element, inner, "", ",")
    lines.append(f"{indent}]{suffix}")
    return lines


def _returned(method: Method) -> dict[str, Any]:
    """What one call returns: the item type of a stream, or the return type."""
    returns = method.returns
    return returns["item"] if returns["kind"] == "stream" else returns


def _models(ref: dict[str, Any], out: set[tuple[str, str]]) -> None:
    kind = ref["kind"]
    if kind == "model":
        # The TOP-LEVEL name, because a nested `X.Y` is imported as `X` and
        # written `X.Y`.
        out.add((ref["module"], ref["qualname"].split(".")[0]))
    elif kind == "list":
        _models(ref["item"], out)
    elif kind == "dict":
        _models(ref["value"], out)
    elif kind == "optional":
        _models(ref["inner"], out)
    elif kind == "stream":
        _models(ref["item"], out)


def _uses_literal(ref: dict[str, Any]) -> bool:
    kind = ref["kind"]
    if kind == "literal":
        return True
    if kind == "list":
        return _uses_literal(ref["item"])
    if kind == "dict":
        return _uses_literal(ref["value"])
    if kind == "optional":
        return _uses_literal(ref["inner"])
    if kind == "stream":
        return _uses_literal(ref["item"])
    return False


def imports_for(desc: Description) -> list[tuple[str, str]]:
    """(module, name) for every model the client's signatures mention, sorted."""
    found: set[tuple[str, str]] = set()
    for method in desc.methods:
        for param in method.params:
            _models(param.type, found)
        _models(method.returns, found)
    return sorted(found)


# Ruff's DEFAULT line length. The generated file lands in a tree whose settings
# nobody here controls, and the default is the narrowest thing it is likely to
# meet, so the emitter targets it rather than this repository's wider limit.
LINE_LENGTH = 88


def _assignment(indent: str, name: str, literal: str) -> list[str]:
    """`NAME = "value"`, wrapped the way ruff wraps it when it is too long.

    A sha256 hash is 71 characters, so `DESCRIPTION_HASH = "sha256:..."` is 92
    and the formatter puts the value in parentheses on its own line. Emitting
    the short form and hoping is how a generated file becomes a diff.
    """
    flat = f"{indent}{name} = {literal}"
    if len(flat) <= LINE_LENGTH:
        return [flat]
    return [f"{indent}{name} = (", f"{indent}    {literal}", f"{indent})"]


# A DEFAULT THAT BUILDS A MODEL IS A TREE UNTIL IT IS LAID OUT, as an annotation is, because ruff
# splits `Item.model_validate({...})` at its brackets, and two linter rules report on the line that
# OPENS the expression they flag: B008 on each call and B006 on a list or dict default. A comment
# after the closing bracket is not honoured, so the layout decides where each suppression goes.


@dataclass(frozen=True)
class _Leaf:
    """A scalar or an empty container, spelled and never split."""

    text: str


@dataclass(frozen=True)
class _Keyed:
    """`"key": value` inside a dict."""

    key: str
    value: _Default


@dataclass(frozen=True)
class _Items:
    """A list or a dict with entries: `[a, b]`, `{"k": v}`."""

    opening: str
    closing: str
    items: tuple[_Default | _Keyed, ...]


@dataclass(frozen=True)
class _Build:
    """`Item.model_validate(argument, strict=False)`: a model built from the value the description
    carries.

    The value is the service's own JSON-mode dump, so a datetime is a string and a set a list.
    Validation is lax so that a model, or a field, declared strict accepts the form it was dumped
    to; for a model that is not strict the argument changes nothing.
    """

    head: str
    argument: _Default


_Default = _Leaf | _Items | _Build

_LAX = "strict=False"


def _value_tree(value: Any) -> _Default:
    """A JSON value as a tree of leaves and containers."""
    if isinstance(value, list) and value:
        return _Items("[", "]", tuple(_value_tree(v) for v in value))
    if isinstance(value, dict) and value:
        return _Items(
            "{", "}", tuple(_Keyed(_literal(k), _value_tree(v)) for k, v in _in_key_order(value))
        )
    return _Leaf(_literal(value))


def _builds_a_model(node: _Default | _Keyed) -> bool:
    if isinstance(node, _Build):
        return True
    if isinstance(node, _Keyed):
        return _builds_a_model(node.value)
    return isinstance(node, _Items) and any(_builds_a_model(item) for item in node.items)


def _default_tree(
    ref: dict[str, Any],
    value: Any,
    aliases: dict[tuple[str, str], str],
    spelling: dict[str, str],
) -> _Default | None:
    """The default written as the models it stands for, or None when no model is in it.

    A parameter typed `Item` whose default is `{"name": "x"}` is a type error for the caller, and
    `inspect.signature` shows a dict where it promised an `Item`. So the default is built:
    `Item.model_validate({"name": "x"})`, and a list or dict of models has each member built. The
    instance is made once, when the client module is imported, and every call that leaves the
    argument out passes that one object; `_encode` only reads it.
    """
    kind = ref["kind"]
    if kind == "model" and isinstance(value, dict):
        name = _flat(_unshadowed(_annotation(ref, aliases), spelling))
        return _Build(f"{name}.model_validate", _value_tree(value))
    if kind == "optional" and value is not None:
        return _default_tree(ref["inner"], value, aliases, spelling)
    if kind == "list" and isinstance(value, list) and value:
        members: list[_Default | _Keyed] = [
            _default_tree(ref["item"], v, aliases, spelling) or _value_tree(v) for v in value
        ]
        node = _Items("[", "]", tuple(members))
        return node if _builds_a_model(node) else None
    if kind == "dict" and isinstance(value, dict) and value:
        entries: list[_Default | _Keyed] = [
            _Keyed(
                _literal(k),
                _default_tree(ref["value"], v, aliases, spelling) or _value_tree(v),
            )
            for k, v in _in_key_order(value)
        ]
        node = _Items("{", "}", tuple(entries))
        return node if _builds_a_model(node) else None
    return None


def _flat_default(node: _Default | _Keyed) -> str:
    if isinstance(node, _Leaf):
        return node.text
    if isinstance(node, _Keyed):
        return f"{node.key}: {_flat_default(node.value)}"
    if isinstance(node, _Build):
        return f"{node.head}({_flat_default(node.argument)}, {_LAX})"
    return node.opening + ", ".join(_flat_default(item) for item in node.items) + node.closing


def _codes_on_one_line(node: _Default | _Keyed, *, top: bool) -> set[str]:
    """The rules that report on a line holding all of `node`."""
    if isinstance(node, _Leaf):
        return set()
    if isinstance(node, _Keyed):
        return _codes_on_one_line(node.value, top=False)
    if isinstance(node, _Build):
        return {"B008"}
    codes = {"B006"} if top else set()
    for item in node.items:
        codes |= _codes_on_one_line(item, top=False)
    return codes


def _suppression(codes: set[str]) -> str:
    return f"  # noqa: {', '.join(sorted(codes))}" if codes else ""


def _opening(node: _Default) -> str:
    """What opens `node` when it is split: the call's head and bracket, or the bracket."""
    if isinstance(node, _Build):
        return f"{node.head}("
    return node.opening if isinstance(node, _Items) else ""


def _default_laid_out(
    node: _Default, indent: str, prefix: str, suffix: str, *, top: bool = False
) -> list[str]:
    """`prefix` + the default + `suffix`, split the way ruff splits it, each suppression on the
    line that opens what it covers.

    The flat form when it fits, comments not measured. Otherwise a call puts its arguments on a
    line of their own, or one to a line, and a list or dict puts one entry per line with a
    trailing comma, which ruff keeps whatever the width.
    """
    flat = f"{indent}{prefix}{_flat_default(node)}{suffix}"
    if len(flat) <= LINE_LENGTH or isinstance(node, _Leaf):
        return [flat + _suppression(_codes_on_one_line(node, top=top))]
    inner = indent + "    "
    if isinstance(node, _Build):
        # Both arguments on one line when they fit, as ruff puts them; otherwise one to a line,
        # each with a trailing comma.
        together = f"{inner}{_flat_default(node.argument)}, {_LAX}"
        arguments = (
            [together]
            if len(together) <= LINE_LENGTH
            else [*_default_laid_out(node.argument, inner, "", ","), f"{inner}{_LAX},"]
        )
        return [
            f"{indent}{prefix}{node.head}(" + _suppression({"B008"}),
            *arguments,
            f"{indent}){suffix}",
        ]
    lines = [f"{indent}{prefix}{node.opening}" + _suppression({"B006"} if top else set())]
    for item in node.items:
        if isinstance(item, _Keyed):
            lines += _default_laid_out(item.value, inner, f"{item.key}: ", ",")
        else:
            lines += _default_laid_out(item, inner, "", ",")
    lines.append(f"{indent}{node.closing}{suffix}")
    return lines


# (prefix, annotation, default, the default's members when it is a container
# ruff would split). `("x: ", int, " = 1", None)`, `("self", None, "", None)`.
_Parameter = tuple[str, "_Annotation | None", str, "tuple[str, list[str], str] | None"]


def _streams(desc: Description) -> bool:
    """Whether any method streams its reply, which the client reads under `contextlib.aclosing`."""
    return any(method.returns["kind"] == "stream" for method in desc.methods)


def _signature(
    name: str,
    params: list[_Parameter],
    returns: _Annotation,
    *,
    suppress_b006: set[int] | None = None,
    built: dict[int, _Default] | None = None,
) -> list[str]:
    """`async def f(a, b) -> R:`, exploded with a trailing comma when too long.

    An annotation too long for its line is split as ruff splits it, and so is
    the return annotation. A parameter whose default is a list or dict splits
    the default first, as ruff does, when the annotation fits on the line that
    opens it.

    A parameter whose index is in `suppress_b006` gets `# noqa: B006` after its
    comma, and its presence forces the exploded form. The generated client
    mirrors the service's signature, so a handler declaring
    `tags: list[str] = ["a", "b"]` produces a mutable default here -- which
    `flake8-bugbear` flags, and this repository selects `B`. The default cannot
    be dropped without changing the client's signature, so it is declared
    deliberate where it appears.

    Exploded because the comment needs a line of its own: on the flat form one
    comment would cover every parameter, including any added later.

    A parameter whose index is in `built` has a default that builds models (see `_default_tree`).
    It is laid out by `_default_laid_out`, which puts `# noqa: B008` (and `B006` for a list or
    dict) on the line that opens each expression ruff reports.
    """
    suppress = suppress_b006 or set()
    built = built or {}
    flat_params = ", ".join(
        f"{prefix}{_flat(annotation) if annotation else ''}{default}"
        for prefix, annotation, default, _ in params
    )
    flat = f"    async def {name}({flat_params}) -> {_flat(returns)}:"
    if not suppress and not built and len(flat) <= LINE_LENGTH:
        return [flat]
    lines = [f"    async def {name}("]
    for index, (prefix, annotation, default, members) in enumerate(params):
        # The comma first: a comment before it is a syntax error.
        note = "  # noqa: B006" if index in suppress else ""
        if annotation is None:
            lines.append(f"        {prefix}{default},{note}")
            continue
        head = f"        {prefix}{_flat(annotation)}"
        if index in built:
            node = built[index]
            if isinstance(annotation, _Name) or len(f"{head} = {_opening(node)}") <= LINE_LENGTH:
                lines += _default_laid_out(
                    node, "        ", f"{prefix}{_flat(annotation)} = ", ",", top=True
                )
            else:
                # A subscripted annotation too long to share its line with the opening of the
                # default is split by ruff first, and the default then continues from the line
                # that closes the annotation (`] = [`, `| None = Item.model_validate(`). A bare
                # name cannot be split, so ruff splits the default instead, however long the name.
                split = _split(annotation, "        ", prefix, " =", False)
                lines += split[:-1]
                lines += _default_laid_out(
                    node, "        ", split[-1].lstrip() + " ", ",", top=True
                )
            continue
        if (
            members is not None
            and len(f"{head}{default},") > LINE_LENGTH
            and len(f"{head} = {members[0]}") <= LINE_LENGTH
        ):
            opening, items, closing = members
            # The suppression goes where ruff reports the default, which is the line that opens
            # it: after the closing bracket it is not honoured, and B006 is reported.
            lines.append(f"{head} = {opening}{note}")
            lines += [f"            {item}," for item in items]
            lines.append(f"        {closing},")
            continue
        lines += _laid_out(annotation, "        ", prefix, f"{default},", comment=note)
    lines += _laid_out(returns, "    ", ") -> ", ":", statement=True)
    return lines


def _class_name(service: str) -> str:
    parts = [p for p in service.replace("-", "_").split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) + "Client"


def _reserved_module_names(desc: Description, *, uses_literal: bool) -> set[str]:
    """Every module-level name the generated file binds other than its models.

    A model sharing one of these silently loses: the two bound BEFORE the model
    imports (`Literal`, `ServiceClient`) break the file loudly, and the ones
    bound AFTER (`SIGNATURES`, the annotation tables, the client class) rebind
    the model's name, so the annotation reaching `_encode` is a dict of hashes
    or a class. That one imports and lints.

    Derived rather than listed at the call site, and pinned by a test that
    reads the emitted source back and compares. Adding a name to `emit` without
    adding it here reopens the hole in silence, which is how it arrived.
    """
    names = {"ServiceClient", "SIGNATURES", "_PARAM_TYPES", "_RETURN_TYPES", "_typing"}
    names.update(f"_Return_{method.name}" for method in desc.methods)
    if _streams(desc):
        names.add("_contextlib")
    if uses_literal:
        names.add("Literal")
    if desc.service:
        names.add(_class_name(desc.service))
    return names


def _refusals(desc: Description) -> list[str]:
    """Everything about this Description a generated file could not express.

    The unimportable-module rule is `unimportable_models`, the same function
    the command's exit 4 uses, rather than a second copy of it here: two
    statements of "which module can a client import" can disagree, and the one
    that disagrees silently is the one nobody is testing.
    """
    offenders: list[str] = []
    cls_name = _class_name(desc.service) if desc.service else ""
    if (
        not desc.service
        or not cls_name.isidentifier()
        or keyword.iskeyword(cls_name)
        or cls_name == "ServiceClient"
    ):
        offenders.append(shown(desc.service) or "<empty>")
    for method in desc.methods:
        for param in method.params:
            offenders += unimportable_models(param.type)
            if (
                not param.name.isidentifier()
                or keyword.iskeyword(param.name)
                or param.name.startswith("_")
                or param.name in ("self", "correlation_id")
            ):
                offenders.append(f"{shown(method.name)}.{shown(param.name)}")
        names = [param.name for param in method.params]
        # Names that are one name once Python reads them: it normalises an identifier to NFKC, so
        # two spellings of one name are a duplicate argument the file cannot be compiled with.
        normalised = [unicodedata.normalize("NFKC", name) for name in names]
        for name, plain in zip(names, normalised, strict=True):
            if normalised.count(plain) > 1:
                offenders.append(f"{shown(method.name)}.{shown(name)}")
        offenders += unimportable_models(method.returns)
        if (
            not method.name.isidentifier()
            or keyword.iskeyword(method.name)
            # Discovery never publishes an underscored handler, so only a reply
            # from something other than a cliffracer service can name one, and
            # every such name on `ServiceClient` is its transport.
            or method.name.startswith("_")
            or method.name in reserved_rpc_method_names()
        ):
            offenders.append(shown(method.name))
    methods = [unicodedata.normalize("NFKC", method.name) for method in desc.methods]
    for method, plain in zip(desc.methods, methods, strict=True):
        if methods.count(plain) > 1:
            offenders.append(shown(method.name))
    for field, text in (("version", desc.version), ("description_hash", desc.description_hash)):
        if "\x00" in str(text):
            offenders.append(f"{field} holds a NUL")
    for method in desc.methods:
        if method.doc and "\x00" in method.doc:
            offenders.append(f"{shown(method.name)} has a doc that holds a NUL")
    return sorted(set(offenders))


def emit(desc: Description, *, namespace: str | None = None) -> str:
    """The source of a client for `desc`, ready to write to a file.

    `namespace` is not part of a description -- a service does not say which
    namespace it was reached in -- so the caller passes the one it used, and
    the client records it as `NAMESPACE`. Nothing is written for None.
    """
    offenders = _refusals(desc)
    if offenders:
        raise CannotEmit("cannot generate a client for: " + ", ".join(offenders))

    uses_literal = any(_uses_literal(p.type) for m in desc.methods for p in m.params) or any(
        _uses_literal(m.returns) for m in desc.methods
    )

    lines = [
        '"""Generated by cliffracer-generate-client. Do not edit.',
        "",
        # Emit metadata across separate lines to adhere to line length constraints.
        f"service: {_escape_docstring_text(desc.service)}",
        f"version: {_escape_docstring_text(desc.version)}",
        f"description: {_escape_docstring_text(desc.description_hash)}",
        *([f"namespace: {_escape_docstring_text(namespace)}"] if namespace else []),
        '"""',
        "",
    ]
    if _streams(desc):
        lines.append("import contextlib as _contextlib")
    lines.append("import typing as _typing")
    if uses_literal:
        lines.append("from typing import Literal")
    lines.append("")
    # The transport import is NOT written yet: it has to sort among the model
    # imports, not ahead of them. `cliffracer.client` and a model module land
    # in the same isort section in a consumer tree -- neither is first-party
    # there -- so a model module sorting before `cliffracer.client`
    # (`acme.schemas`, `app.models`, anything a-b) left the block unsorted and
    # `ruff check` reported I001, which this repository selects. The formatter
    # does not sort imports, so `ruff format --check` returned 0 throughout and
    # only the lint gate ever saw it.
    import_lines: list[tuple[str, str]] = [
        ("cliffracer.client", "from cliffracer.client import ServiceClient")
    ]
    by_module: dict[str, list[str]] = {}
    name_to_modules: dict[str, list[str]] = {}
    aliases: dict[tuple[str, str], str] = {}

    reserved = _reserved_module_names(desc, uses_literal=uses_literal)

    for module, name in imports_for(desc):
        by_module.setdefault(module, []).append(name)
        if module not in name_to_modules.setdefault(name, []):
            name_to_modules[name].append(module)

    # Which imports need an alias, decided before any alias is built: a model
    # keeping its own name takes that name, and an alias must then avoid it.
    needs_alias = {
        (module, name)
        for module in by_module
        for name in by_module[module]
        if len(name_to_modules[name]) > 1 or name in reserved
    }
    taken = set(reserved) | {
        name
        for module in by_module
        for name in by_module[module]
        if (module, name) not in needs_alias
    }

    # Sort modules first to satisfy ruff I001
    for module in sorted(by_module.keys(), key=_module_order):
        names = by_module[module]
        grouped_names = []
        aliased_names: list[tuple[str, str]] = []
        for name in sorted(names, key=_member_order):
            if (module, name) in needs_alias:
                # Two models of the same name, or a model named after something
                # this file already binds. Same remedy either way: import it
                # under a prefixed alias, so the name the annotations use is one
                # nothing else in the file can take.
                #
                # The alias is then checked against everything already bound,
                # because PREFIXING CAN LAND ON A TAKEN NAME TOO: two modules
                # `m` and `n` exporting `SClient`, for a service whose client
                # class is `MSClient`, alias `m.SClient` to exactly that. The
                # file imported and behaved, and an outside importer asking for
                # `MSClient` got the client class. Prefix again until free.
                #
                # Each pass makes the alias longer, so no candidate repeats,
                # and only the names in `taken` can block one: a free alias is
                # found within len(taken) + 1 passes. Reaching the end refuses
                # by name rather than looping.
                alias = f"{_module_prefix(module)}{name}"
                for _ in range(len(taken) + 1):
                    if alias not in taken:
                        break
                    alias = f"{_module_prefix(module)}{alias}"
                else:
                    raise CannotEmit(
                        f"no free alias for {name!r} imported from {module!r}: "
                        f"prefixing it {len(taken) + 1} times landed only on taken names"
                    )
                taken.add(alias)
                aliases[(module, name)] = alias
                aliased_names.append((name, f"{name} as {alias}"))
            else:
                aliases[(module, name)] = name
                grouped_names.append(name)
        # Ruff's isort writes an `as` import as a statement of its own, with the plain names of the
        # module in another: `from m import Item as MItem, Order` is reported as I001. The
        # statements of one module are ordered by their first name, in the order of names above,
        # so the plain statement comes first only when its first name does. The module sort below
        # is stable.
        keyed = ([(grouped_names[0], grouped_names)] if grouped_names else []) + [
            (name, [text]) for name, text in aliased_names
        ]
        for _, statement in sorted(keyed, key=lambda item: _member_order(item[0])):
            one_line = f"from {module} import {', '.join(statement)}"
            if len(one_line) <= LINE_LENGTH:
                import_lines.append((module, one_line))
            else:
                # Parenthesised, one name per line with a trailing comma, as ruff
                # formats an import too long for its line. Still one entry, so the
                # module sort below moves it whole.
                wrapped = "\n".join(
                    [f"from {module} import (", *(f"    {n}," for n in statement), ")"]
                )
                import_lines.append((module, wrapped))

    # In ruff's isort order, which is not Python's `sorted`: case-insensitive,
    # so `Zmodels.m` does not land ahead of `cliffracer.client` because `Z` < `c`
    # in ASCII, and natural, so `app.v2` comes before `app.v10`. Either
    # difference makes ruff report I001 on the block. `sorted` is stable and the
    # modules arrive already in this order, so two modules equal under the key
    # come out the same way round whatever order the Description listed them.
    lines += [line for _, line in sorted(import_lines, key=lambda kv: _module_order(kv[0]))]

    if desc.methods:
        lines += ["", "SIGNATURES = {"]
        for method in desc.methods:
            lines.append(f"    {_literal(method.name)}: {_literal(method.signature_hash)},")
        lines.append("}")
    else:
        # `{}` rather than an empty exploded mapping: with no entries there is no
        # trailing comma to hold the shape, so ruff collapses it and the file is
        # a diff on first contact with the formatter. Same reason `self._call`
        # writes `{}` for a method with no parameters.
        lines += ["", "SIGNATURES: dict[str, str] = {}"]

    # Annotations are bound HERE, at module level, and never written again
    # inside a method body. A body is the one place a parameter name is in
    # scope, so an annotation re-evaluated there means whatever the caller
    # passed: `tag(self, list: list[str])` used to emit
    # `self._encode(list, list[str])`, subscripting the argument.
    #
    # Raising was the lucky case. A parameter shadowing a SUBSCRIPTED
    # annotation raises TypeError; one shadowing a bare name substitutes the
    # argument for the type in silence, and the wrong type reaches `_encode`
    # and `_call`. A parameter cannot collide with these names because
    # `_refusals` rejects a leading underscore.
    # `{}` rather than an empty exploded mapping: with no entries there is no
    # trailing comma to hold the shape open, so ruff collapses it and the
    # generated file is a diff the moment anyone runs the formatter. Same
    # reason an empty `SIGNATURES` is written that way, and the same reason
    # `self._call` writes `{}` for a method with no parameters -- one rule,
    # three places that would each have reintroduced it.
    if desc.methods:
        lines += ["", "_PARAM_TYPES: dict[str, dict[str, _typing.Any]] = {"]
        for method in desc.methods:
            if not method.params:
                lines.append(f"    {_literal(method.name)}: {{}},")
                continue
            lines.append(f"    {_literal(method.name)}: {{")
            for param in method.params:
                lines += _laid_out(
                    _annotation(param.type, aliases), "        ", f"{_literal(param.name)}: ", ","
                )
            lines.append("    },")
        lines += ["}", "", "_RETURN_TYPES: dict[str, _typing.Any] = {"]
        for method in desc.methods:
            lines += _laid_out(
                _annotation(_returned(method), aliases), "    ", f"{_literal(method.name)}: ", ","
            )
        lines += ["}"]
    else:
        lines += [
            "",
            "_PARAM_TYPES: dict[str, dict[str, _typing.Any]] = {}",
            "",
            "_RETURN_TYPES: dict[str, _typing.Any] = {}",
        ]
    if desc.methods:
        lines.append("")
        for method in desc.methods:
            lines += _laid_out(
                _annotation(_returned(method), aliases),
                "",
                f"type _Return_{method.name} = ",
                "",
                statement=True,
            )
    # A `def` evaluates its annotations in the class body, where the methods defined before it and
    # the class attributes are names: `async def list(...)` makes `list[str]` in the next method
    # the method, and the import raises `TypeError`. A name that is both bound in the class and
    # read by one of its annotations is therefore read through a module-level alias, bound before
    # the class and so never shadowed.
    class_names = {"SERVICE", "VERSION", "DESCRIPTION_HASH", "SIGNATURES"}
    if namespace:
        class_names.add("NAMESPACE")
    class_names.update(method.name for method in desc.methods)
    read_by_a_signature: set[str] = set()
    for method in desc.methods:
        read_by_a_signature |= _names_in(_annotation(method.returns, aliases))
        for param in method.params:
            read_by_a_signature |= _names_in(_annotation(param.type, aliases))
    spelling = {name: f"_Unshadowed_{name}" for name in sorted(read_by_a_signature & class_names)}
    if "Literal" in spelling:
        raise CannotEmit(
            f"a method named {shown('Literal')} in a client whose signatures use `Literal`: "
            f"the annotation is written `Literal[...]`, which the method would shadow"
        )
    if spelling:
        lines.append("")
        lines += [f"{alias} = {name}" for name, alias in spelling.items()]
    lines += ["", "", f"class {_class_name(desc.service)}(ServiceClient):"]
    lines += _assignment("    ", "SERVICE", _literal(desc.service))
    if namespace:
        lines += _assignment("    ", "NAMESPACE", _literal(namespace))
    lines += _assignment("    ", "VERSION", _literal(desc.version))
    lines += _assignment("    ", "DESCRIPTION_HASH", _literal(desc.description_hash))
    lines.append("    SIGNATURES = SIGNATURES")
    for method in desc.methods:
        params: list[_Parameter] = [("self", None, "", None)]
        seen_default = False
        star_emitted = False
        mutable_defaults: set[int] = set()
        built_defaults: dict[int, _Default] = {}
        for param in method.params:
            if seen_default and not param.has_default and not star_emitted:
                params.append(("*", None, "", None))
                star_emitted = True
            default = ""
            members: tuple[str, list[str], str] | None = None
            if param.has_default:
                seen_default = True
                default = f" = {_literal(param.default)}"
                tree = (
                    _default_tree(param.type, param.default, aliases, spelling)
                    if param.rebuildable is True
                    else None
                )
                if tree is not None:
                    built_defaults[len(params)] = tree
                    default = f" = {_flat_default(tree)}"
                elif isinstance(param.default, list | dict | set):
                    mutable_defaults.add(len(params))
                    if isinstance(param.default, list) and param.default:
                        members = ("[", [_literal(v) for v in param.default], "]")
                    elif isinstance(param.default, dict) and param.default:
                        members = (
                            "{",
                            [
                                f"{_literal(k)}: {_literal(v)}"
                                for k, v in _in_key_order(param.default)
                            ],
                            "}",
                        )
            params.append(
                (
                    f"{param.name}: ",
                    _unshadowed(_annotation(param.type, aliases), spelling),
                    default,
                    members,
                )
            )
        lines.append("")
        lines += _signature(
            method.name,
            params,
            _unshadowed(_annotation(method.returns, aliases), spelling),
            suppress_b006=mutable_defaults,
            built=built_defaults,
        )
        if method.doc:
            lines += _docstring_lines(method.doc, "        ")
        # Exploded, with a trailing comma on every level that has entries.
        # Ruff's magic trailing comma keeps this shape whatever the line
        # length, so a long signature cannot make the output need reformatting.
        streams = method.returns["kind"] == "stream"
        lines.append(
            "        _items = self._stream(" if streams else "        _result = await self._call("
        )
        lines.append(f"            {_literal(method.name)},")
        if method.params:
            lines.append("            {")
            for param in method.params:
                # Wrapped the way ruff wraps it when the one-line form is too
                # long. A table lookup is much wider than the annotation it
                # replaced -- `_PARAM_TYPES["m"]["warehouse_identifier"]` where
                # `str` used to stand -- so a long parameter name that fitted
                # before now crosses the limit, and emitting the short form and
                # hoping is how a generated file becomes a diff.
                key = f"_PARAM_TYPES[{_literal(method.name)}][{_literal(param.name)}]"
                one_line = (
                    f"                {_literal(param.name)}: self._encode({param.name}, {key}),"
                )
                arguments = f"                    {param.name}, {key}"
                if len(one_line) <= LINE_LENGTH:
                    lines.append(one_line)
                elif len(arguments) <= LINE_LENGTH:
                    lines.append(f"                {_literal(param.name)}: self._encode(")
                    lines.append(arguments)
                    lines.append("                ),")
                else:
                    # Too long even on a line of their own: one argument per
                    # line with a trailing comma, and the lookup split at its
                    # last bracket when it alone does not fit, as ruff does.
                    lines.append(f"                {_literal(param.name)}: self._encode(")
                    lines.append(f"                    {param.name},")
                    if len(f"                    {key},") <= LINE_LENGTH:
                        lines.append(f"                    {key},")
                    else:
                        lines.append(f"                    _PARAM_TYPES[{_literal(method.name)}][")
                        lines.append(f"                        {_literal(param.name)}")
                        lines.append("                    ],")
                    lines.append("                ),")
            lines.append("            },")
        else:
            lines.append("            {},")
        lines.append(f"            _RETURN_TYPES[{_literal(method.name)}],")
        # The call validates against the runtime table. Module-level aliases
        # keep cast types outside parameter scope for both Python and mypy.
        # Leading underscores are forbidden in public parameter names.
        return_type = f"_Return_{method.name}"
        if streams:
            # Each item is validated against `_RETURN_TYPES`, which holds the item type of a
            # stream, as it is yielded. Held under aclosing, so closing this method's generator
            # closes the stream, and its reply inbox, at once.
            lines += [
                "        )",
                "        async with _contextlib.aclosing(_items):",
                "            async for _item in _items:",
            ]
            yielded = f"                yield _typing.cast({return_type}, _item)"
            if len(yielded) <= LINE_LENGTH:
                lines.append(yielded)
            else:
                lines += [
                    "                yield _typing.cast(",
                    f"                    {return_type}, _item",
                    "                )",
                ]
            continue
        lines.append("        )")
        cast = f"        return _typing.cast({return_type}, _result)"
        if len(cast) <= LINE_LENGTH:
            lines.append(cast)
        else:
            lines += [
                "        return _typing.cast(",
                f"            {return_type}, _result",
                "        )",
            ]
    return "\n".join(lines).rstrip() + "\n"

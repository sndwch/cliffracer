"""Every untrusted string the emitter writes stays inside the quotes it is written in.

The generated file is Python that a person is told to commit and import, and in broker mode the
whole `Description` is JSON from a describe reply. Four strings of it land in quoted positions: the
version, the description hash and the namespace in the header docstring, and a method's doc in the
method body. A string that closes its quotes early turns the rest of itself into a statement of
the generated module.

Each case swaps one field for a breakout string and compares the emitted module with the one a
harmless value produced, with every string constant blanked: the same tree means nothing but a
string changed, and a spelled-out `MARKER = ...` statement would be an extra node. The field's
own text is then read back out of the string it was written into.
"""

import ast
import copy
import inspect

import pytest

from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import Description

pytestmark = pytest.mark.unit

DESCRIPTION = {
    "service": "orders",
    "version": "1.4.0",
    "description_hash": "sha256:abc",
    "methods": [
        {
            "name": "create",
            "doc": "Create an order.",
            "signature_hash": "sha256:s1",
            "params": [{"name": "note", "type": {"kind": "scalar", "name": "str"}, "default": ""}],
            "returns": {"kind": "scalar", "name": "str"},
        }
    ],
}

# What closes a triple-quoted string early, or eats the quote that closes it.
BREAKOUTS = {
    "triple-quote-then-statement": 'x"""; MARKER = 1; """',
    # No quote or backslash at the end and no newline: the only thing wrong with it is the middle.
    "triple-quote-mid-line": 'x"""; MARKER = 1  # y',
    "triple-quote-across-lines": 'x"""\nMARKER = 1\n"""',
    "trailing-backslash": "x\\",
    "trailing-quote": 'x"',
    "backslash-before-triple-quote": 'x\\"""; MARKER = 1; """',
}

A_LONG_DOC_PREFIX = "A sentence long enough that the doc has to be wrapped onto more lines. " * 4


class _BlankStrings(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        return ast.Constant(value="" if isinstance(node.value, str) else node.value)


def _shape(source: str) -> str:
    """The module's tree with every string constant emptied."""
    return ast.dump(_BlankStrings().visit(ast.parse(source)))


def _described(*, field: str | None = None, value: str | None = None) -> Description:
    data = copy.deepcopy(DESCRIPTION)
    if field == "doc":
        data["methods"][0]["doc"] = value
    elif field is not None:
        data[field] = value
    return Description.from_dict(data)


def _header_docstring(source: str) -> str:
    docstring = ast.get_docstring(ast.parse(source), clean=False)
    assert docstring is not None, source
    return docstring


def _method_docstring(source: str) -> str:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create":
            docstring = ast.get_docstring(node, clean=True)
            assert docstring is not None, ast.dump(node)
            return docstring
    raise AssertionError("the generated client has no `create` method")


@pytest.mark.parametrize("breakout", BREAKOUTS.values(), ids=BREAKOUTS.keys())
@pytest.mark.parametrize("field", ["version", "description_hash"])
def test_a_header_field_stays_inside_the_header_docstring(field, breakout):
    clean = emit(_described())
    evil = emit(_described(field=field, value=breakout))

    assert _shape(evil) == _shape(clean), f"{field}={breakout!r} changed the module's structure"
    assert f"{field.replace('_hash', '')}: {breakout}" in _header_docstring(evil), evil


@pytest.mark.parametrize("breakout", BREAKOUTS.values(), ids=BREAKOUTS.keys())
def test_the_namespace_stays_inside_the_header_docstring_and_its_literal(breakout):
    clean = emit(_described(), namespace="prod")
    evil = emit(_described(), namespace=breakout)

    assert _shape(evil) == _shape(clean), f"namespace={breakout!r} changed the module's structure"
    assert f"namespace: {breakout}" in _header_docstring(evil), evil
    namespaces = [
        node.value.value
        for node in ast.walk(ast.parse(evil))
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "NAMESPACE" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    assert namespaces == [breakout], namespaces


@pytest.mark.parametrize("breakout", BREAKOUTS.values(), ids=BREAKOUTS.keys())
def test_a_method_doc_stays_inside_its_docstring(breakout):
    clean = emit(_described())
    evil = emit(_described(field="doc", value=breakout))

    assert _shape(evil) == _shape(clean), f"doc={breakout!r} changed the module's structure"
    # A doc that spans lines is indented into the method body; `clean` takes that back out.
    assert _method_docstring(evil) == inspect.cleandoc(breakout), evil


@pytest.mark.parametrize("breakout", BREAKOUTS.values(), ids=BREAKOUTS.keys())
def test_a_method_doc_that_is_wrapped_stays_inside_its_docstring(breakout):
    """A doc too long for one line takes the wrapping path, which escapes for itself."""
    doc = A_LONG_DOC_PREFIX + breakout
    clean = emit(_described())
    evil = emit(_described(field="doc", value=doc))

    assert _shape(evil) == _shape(clean), f"a wrapped doc ending {breakout!r} changed the structure"
    assert "MARKER" not in {
        node.id for node in ast.walk(ast.parse(evil)) if isinstance(node, ast.Name)
    }
    assert A_LONG_DOC_PREFIX.split()[0] in _method_docstring(evil)


def _breaks(source: str, clean_shape: str) -> bool:
    try:
        return _shape(source) != clean_shape
    except SyntaxError:
        return True


def test_CONTROL_the_breakouts_break_a_module_when_written_unescaped():
    """The tests above pass for any emitter if the breakouts cannot break anything.

    Each breakout is written into the generated text the way an emitter that did not escape would
    write it: into the header, where only the ones that carry a statement or a closing quote
    matter (a trailing backslash or quote is harmless there), and into the one-line method
    docstring, where every one of them breaks the module.
    """
    clean = emit(_described())
    clean_shape = _shape(clean)
    header, one_line = [], []
    for name, breakout in BREAKOUTS.items():
        if _breaks(
            clean.replace("description: sha256:abc", f"description: {breakout}"), clean_shape
        ):
            header.append(name)
        if _breaks(clean.replace('"""Create an order."""', f'"""{breakout}"""'), clean_shape):
            one_line.append(name)

    assert header == [
        "triple-quote-then-statement",
        "triple-quote-mid-line",
        "triple-quote-across-lines",
        "backslash-before-triple-quote",
    ], header
    assert one_line == list(BREAKOUTS), one_line

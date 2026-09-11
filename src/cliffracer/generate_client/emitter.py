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
import json
import keyword
from typing import Any

from cliffracer.core.typed_rpc import _RESERVED_RPC_METHOD_NAMES, unimportable_models
from cliffracer.introspect import Description

_SCALAR_TEXT = {"str": "str", "int": "int", "float": "float", "bool": "bool", "none": "None"}


class CannotEmit(Exception):
    """The Description names something the generator refuses to express."""


def _literal(value: Any) -> str:
    """A Python literal for a JSON-shaped value, formatted the way ruff wants.

    `repr` gives `'a'` where ruff format gives `"a"`, so every emitted string
    would be reformatted on first contact with the formatter. Booleans and None
    still go through `repr`, because `json.dumps` would spell them `true` and
    `null`.
    """
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, str):
        return json.dumps(value)
    return repr(value)


def _docstring(doc: str) -> str:
    """A one-line docstring literal. Triple-quoted, because that is what a
    reader expects and what every style guide in reach asks for; a doc that
    could break out of the quotes falls back to a plain double-quoted string,
    which is still a docstring and is still safe."""
    if '"""' in doc or doc.endswith('"') or "\\" in doc or "\n" in doc:
        return _literal(doc)
    return f'"""{doc}"""'


def _module_prefix(module: str) -> str:
    return "".join(p.capitalize() for p in module.replace(".", "_").split("_"))


def _escape_docstring_text(text: str) -> str:
    """Escape backslashes and triple quotes so the text cannot break out of a triple-quoted docstring."""
    return text.replace("\\", "\\\\").replace('"""', r"\"\"\"")


def annotation_text(ref: dict[str, Any], aliases: dict[tuple[str, str], str] | None = None) -> str:
    kind = ref["kind"]
    if kind == "scalar":
        return _SCALAR_TEXT[ref["name"]]
    if kind == "model":
        if aliases and (ref["module"], ref["qualname"]) in aliases:
            return aliases[(ref["module"], ref["qualname"])]
        module_prefix = "".join(p.capitalize() for p in ref["module"].split(".")[-1].split("_"))
        return f"{module_prefix}{ref['qualname']}"
    if kind == "list":
        return f"list[{annotation_text(ref['item'], aliases)}]"
    if kind == "dict":
        return f"dict[str, {annotation_text(ref['value'], aliases)}]"
    if kind == "optional":
        return f"{annotation_text(ref['inner'], aliases)} | None"
    if kind == "literal":
        if not ref["values"]:
            return "Literal[()]"
        return "Literal[" + ", ".join(_literal(v) for v in ref["values"]) + "]"
    raise CannotEmit(f"unknown TypeRef kind {kind!r}")


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


def _signature(name: str, params: list[str], returns: str) -> list[str]:
    """`async def f(a, b) -> R:`, exploded with a trailing comma when too long."""
    flat = f"    async def {name}({', '.join(params)}) -> {returns}:"
    if len(flat) <= LINE_LENGTH:
        return [flat]
    lines = [f"    async def {name}("]
    lines += [f"        {p}," for p in params]
    lines.append(f"    ) -> {returns}:")
    return lines


def _class_name(service: str) -> str:
    parts = [p for p in service.replace("-", "_").split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) + "Client"


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
        offenders.append(desc.service or "<empty>")
    for method in desc.methods:
        for param in method.params:
            offenders += unimportable_models(param.type)
            if (
                not param.name.isidentifier()
                or keyword.iskeyword(param.name)
                or param.name.startswith("_")
                or param.name in ("self", "correlation_id")
            ):
                offenders.append(f"{method.name}.{param.name}")
        offenders += unimportable_models(method.returns)
        if (
            not method.name.isidentifier()
            or keyword.iskeyword(method.name)
            or method.name in _RESERVED_RPC_METHOD_NAMES
        ):
            offenders.append(method.name)
    return sorted(set(offenders))


def emit(desc: Description) -> str:
    """The source of a client for `desc`, ready to write to a file."""
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
        '"""',
        "",
    ]
    if uses_literal:
        lines += ["from typing import Literal", ""]
    lines.append("from cliffracer.client import ServiceClient")
    by_module: dict[str, list[str]] = {}
    name_to_modules: dict[str, list[str]] = {}
    aliases: dict[tuple[str, str], str] = {}

    for module, name in imports_for(desc):
        by_module.setdefault(module, []).append(name)
        if module not in name_to_modules.setdefault(name, []):
            name_to_modules[name].append(module)

    # Sort modules first to satisfy ruff I001
    for module in sorted(by_module.keys()):
        names = by_module[module]
        grouped_names = []
        for name in sorted(names):
            if len(name_to_modules[name]) > 1:
                # Collision: disambiguate
                prefix = _module_prefix(module)
                alias = f"{prefix}{name}"
                aliases[(module, name)] = alias
                grouped_names.append(f"{name} as {alias}")
            else:
                aliases[(module, name)] = name
                grouped_names.append(name)
        names_str = ", ".join(grouped_names)
        lines.append(f"from {module} import {names_str}")

    lines += ["", "SIGNATURES = {"]
    for method in desc.methods:
        lines.append(f"    {_literal(method.name)}: {_literal(method.signature_hash)},")
    lines += ["}", "", "", f"class {_class_name(desc.service)}(ServiceClient):"]
    lines += _assignment("    ", "SERVICE", _literal(desc.service))
    lines += _assignment("    ", "VERSION", _literal(desc.version))
    lines += _assignment("    ", "DESCRIPTION_HASH", _literal(desc.description_hash))
    lines.append("    SIGNATURES = SIGNATURES")
    for method in desc.methods:
        params = ["self"]
        seen_default = False
        star_emitted = False
        for param in method.params:
            if seen_default and not param.has_default and not star_emitted:
                params.append("*")
                star_emitted = True
            text = f"{param.name}: {annotation_text(param.type, aliases)}"
            if param.has_default:
                seen_default = True
                text += f" = {_literal(param.default)}"
            params.append(text)
        lines.append("")
        lines += _signature(method.name, params, annotation_text(method.returns, aliases))
        if method.doc:
            lines.append(f"        {_docstring(method.doc)}")
        # Exploded, with a trailing comma on every level that has entries.
        # Ruff's magic trailing comma keeps this shape whatever the line
        # length, so a long signature cannot make the output need reformatting.
        lines.append("        return await self._call(")
        lines.append(f"            {_literal(method.name)},")
        if method.params:
            lines.append("            {")
            for param in method.params:
                lines.append(
                    f"                {_literal(param.name)}: "
                    f"self._encode({param.name}, {annotation_text(param.type, aliases)}),"
                )
            lines.append("            },")
        else:
            lines.append("            {},")
        lines.append(f"            {annotation_text(method.returns, aliases)},")
        lines.append("        )")
    return "\n".join(lines).rstrip() + "\n"

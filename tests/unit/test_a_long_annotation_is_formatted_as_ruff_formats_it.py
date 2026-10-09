"""A generated client is already formatted for ruff, however long its annotations.

The emitter lays an annotation out itself: flat when it fits, otherwise split at
its brackets and before `| None`, as ruff splits one. It does not run ruff at
emit time -- that would make the output depend on a tool the consumer may not
have, and on its version -- so ruff is only the check, here.

The check is a corpus rather than a handful of examples. Ruff's layout depends on
where an annotation sits: a table entry, a return alias, a parameter with or
without a default, a return annotation. A seeded generator builds 200
descriptions mixing every annotation kind, nesting, name length and default,
and one `ruff format --check` over all of them must find nothing to change. The
corpus is asserted to hold annotations too long for their line in each of those
positions, so a generator that drifted towards short names cannot make this
pass by having nothing to split.

The layout is held to the ruff this repository pins. A change in ruff's style
reds this by design, which is where it should be learned.
"""

from __future__ import annotations

import hashlib
import keyword
import random
import re
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cliffracer.generate_client import emitter
from cliffracer.generate_client.emitter import LINE_LENGTH, emit
from cliffracer.introspect import Description

pytestmark = pytest.mark.unit

SEED = 1567
DESCRIPTIONS = 200


def _word(rng: random.Random, low: int, high: int) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(low, high)))


def _ref(rng: random.Random, depth: int, models: list[tuple[str, str]]) -> dict[str, Any]:
    kinds = ["scalar", "literal", "model"] + (["list", "dict", "optional"] * 2 if depth < 3 else [])
    kind = rng.choice(kinds)
    if kind == "scalar":
        return {"kind": "scalar", "name": rng.choice(["str", "int", "float", "bool"])}
    if kind == "literal":
        values = [
            _word(rng, 2, 28) if rng.random() < 0.8 else rng.randint(0, 999)
            for _ in range(rng.randint(1, 6))
        ]
        return {"kind": "literal", "values": list(dict.fromkeys(values))}
    if kind == "model":
        # A module per model and a distinct name, so no import is aliased and
        # the annotation's flat length below is the one the file carries.
        name = _word(rng, 4, 60).capitalize() + str(len(models))
        module = f"acme.m{len(models)}"
        models.append((module, name))
        return {"kind": "model", "module": module, "qualname": name}
    if kind == "list":
        return {"kind": "list", "item": _ref(rng, depth + 1, models)}
    if kind == "dict":
        return {"kind": "dict", "value": _ref(rng, depth + 1, models)}
    inner = _ref(rng, depth + 1, models)
    while inner["kind"] == "optional":
        inner = _ref(rng, depth + 1, models)
    return {"kind": "optional", "inner": inner}


# By construction, not by chance: every tenth description has a long method and
# parameter names, whose `self._encode(name, _PARAM_TYPES["method"]["name"])`
# line is over the limit whatever the random draw, and every tenth, offset by
# five, imports five models from one module, whose one-line import is too.
LONG_NAMES_EVERY = 10
SHARED_MODULE_EVERY = 10
SHARED_MODULE = "acme.schemas"
SHARED_MODELS = 5


def _hash(*parts: object) -> str:
    return "sha256:" + hashlib.sha256(repr(parts).encode()).hexdigest()


def _description(rng: random.Random, index: int) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    models: list[tuple[str, str]] = []
    methods = []
    long_names = index % LONG_NAMES_EVERY == 0
    if index % SHARED_MODULE_EVERY == 5:
        params = []
        for number in range(SHARED_MODELS):
            name = _word(rng, 12, 24).capitalize() + str(len(models))
            models.append((SHARED_MODULE, name))
            params.append(
                {
                    "name": f"p{number}",
                    "type": {"kind": "model", "module": SHARED_MODULE, "qualname": name},
                }
            )
        methods.append(
            {
                "name": "shared",
                "doc": "",
                "signature_hash": _hash(index, "shared"),
                "params": params,
                "returns": {"kind": "scalar", "name": "int"},
            }
        )
    for number in range(rng.randint(1, 3)):
        params = []
        names: set[str] = set()
        for _ in range(rng.randint(0, 3)):
            name = _word(rng, 25, 30) if long_names else _word(rng, 1, 10)
            if name in names or keyword.iskeyword(name) or keyword.issoftkeyword(name):
                continue
            names.add(name)
            param: dict[str, Any] = {"name": name, "type": _ref(rng, 0, models)}
            roll = rng.random()
            if roll < 0.15:
                param["default"] = "x"
            elif roll < 0.25:
                param["default"] = ["a", "b"]
            elif roll < 0.3:
                param["default"] = None
            params.append(param)
        methods.append(
            {
                "name": f"m{number}_{_word(rng, 30, 42) if long_names else _word(rng, 1, 20)}",
                "doc": "",
                "signature_hash": _hash(index, number),
                "params": params,
                "returns": _ref(rng, 0, models),
            }
        )
    description = {
        "service": f"svc{index}",
        "version": "1",
        "description_hash": _hash(index),
        "methods": methods,
    }
    return description, models


def _corpus() -> list[tuple[Description, dict[tuple[str, str], str]]]:
    rng = random.Random(SEED)
    corpus = []
    for index in range(DESCRIPTIONS):
        raw, models = _description(rng, index)
        corpus.append((Description.from_dict(raw), {m: m[1] for m in models}))
    return corpus


CORPUS = _corpus()


# IMPORT ORDER, one case per rule of ruff's isort measured on 0.12.1. Each lists
# (module, model) pairs, and `plain` is the order a plain Python sort gives the
# thing the rule orders -- asserted below to differ from ruff's where the rule
# is one a plain sort breaks, so each case can only pass on the rule itself.
IMPORT_ORDER_CASES: dict[str, dict[str, Any]] = {
    "natural module order": {
        "models": [("acme.api.v10", "Order"), ("acme.api.v2", "Receipt")],
        "ruff": ["acme.api.v2", "acme.api.v10"],
        "plain": sorted(["acme.api.v10", "acme.api.v2"], key=str.lower),
    },
    "a leading zero compares digit by digit": {
        "models": [("acme.v4", "Order"), ("acme.v05", "Receipt")],
        "ruff": ["acme.v05", "acme.v4"],
        "plain": sorted(["acme.v4", "acme.v05"], key=lambda m: int(m.rsplit("v", 1)[1])),
    },
    "case-insensitive module order": {
        "models": [("Zeta.models", "Order"), ("alpha.models", "Receipt")],
        "ruff": ["alpha.models", "Zeta.models"],
        "plain": sorted(["Zeta.models", "alpha.models"]),
    },
    "dot before underscore": {
        "models": [("acme_x", "Order"), ("acme.x", "Receipt")],
        "ruff": ["acme.x", "acme_x"],
        "plain": sorted(["acme_x", "acme.x"], key=lambda m: m.replace(".", "{")),
    },
    "constants, then classes, then variables": {
        "models": [("acme.kinds", n) for n in ("alpha_value", "Zeta", "Alpha", "ZULU")],
        "ruff": ["ZULU", "Alpha", "Zeta", "alpha_value"],
        "plain": sorted(["alpha_value", "Zeta", "Alpha", "ZULU"], key=str.lower),
    },
    "natural order within classes": {
        "models": [("acme.items", "Item10"), ("acme.items", "Item2")],
        "ruff": ["Item2", "Item10"],
        "plain": sorted(["Item10", "Item2"]),
    },
    "case-insensitive order within classes": {
        "models": [("acme.letters", "ItemB"), ("acme.letters", "Itema")],
        "ruff": ["Itema", "ItemB"],
        "plain": sorted(["ItemB", "Itema"]),
    },
}


def _import_order_description(case: dict[str, Any]) -> Description:
    params = [
        {"name": f"p{number}", "type": {"kind": "model", "module": module, "qualname": name}}
        for number, (module, name) in enumerate(case["models"])
    ]
    return Description.from_dict(
        {
            "service": "sorting",
            "version": "1",
            "description_hash": _hash("sorting"),
            "methods": [
                {
                    "name": "go",
                    "doc": "",
                    "signature_hash": _hash("go"),
                    "params": params,
                    "returns": {"kind": "scalar", "name": "int"},
                }
            ],
        }
    )


def _ruff_format_check(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "--isolated", "--no-cache", "."],
        capture_output=True,
        text=True,
        cwd=directory,
    )


def _write_corpus(directory: Path) -> None:
    for index, (description, _) in enumerate(CORPUS):
        (directory / f"client_{index:03d}.py").write_text(emit(description))


def _flat(ref: dict[str, Any], aliases: dict[tuple[str, str], str]) -> str:
    return emitter.annotation_text(ref, aliases)


def test_the_corpus_holds_an_annotation_too_long_for_its_line_in_every_position():
    over = {"param table": 0, "return table": 0, "alias": 0, "parameter": 0, "return": 0}
    for description, aliases in CORPUS:
        for method in description.methods:
            returns = _flat(method.returns, aliases)
            over["return table"] += len(f'    "{method.name}": {returns},') > LINE_LENGTH
            over["alias"] += len(f"type _Return_{method.name} = {returns}") > LINE_LENGTH
            over["return"] += len(f"    ) -> {returns}:") > LINE_LENGTH
            for param in method.params:
                annotation = _flat(param.type, aliases)
                over["param table"] += len(f'        "{param.name}": {annotation},') > LINE_LENGTH
                over["parameter"] += len(f"        {param.name}: {annotation},") > LINE_LENGTH
    assert all(count >= 10 for count in over.values()), over


def _encode_lines(method_name: str, param_name: str) -> dict[str, str]:
    """The `_encode` call's three one-line forms, each the emitter's next fallback."""
    key = f'_PARAM_TYPES["{method_name}"]["{param_name}"]'
    return {
        "call": f'                "{param_name}": self._encode({param_name}, {key}),',
        "arguments": f"                    {param_name}, {key}",
        "lookup": f"                    {key},",
    }


def test_every_constructed_long_name_puts_its_encode_call_over_the_line():
    """By construction: a long-names description's parameters all overflow."""
    checked = 0
    for index, (description, _) in enumerate(CORPUS):
        if index % LONG_NAMES_EVERY:
            continue
        for method in description.methods:
            for param in method.params:
                forms = _encode_lines(method.name, param.name)
                assert len(forms["call"]) > LINE_LENGTH, forms["call"]
                assert len(forms["arguments"]) > LINE_LENGTH, forms["arguments"]
                checked += 1
    assert checked >= 10, checked


def test_the_corpus_holds_an_encode_lookup_too_long_for_its_own_line():
    """The last fallback, the lookup split at its bracket, is reached too."""
    over = sum(
        len(_encode_lines(method.name, param.name)["lookup"]) > LINE_LENGTH
        for description, _ in CORPUS
        for method in description.methods
        for param in method.params
    )
    assert over >= 10, over


def test_every_constructed_shared_module_puts_its_import_over_the_line():
    """By construction: five models from one module overflow the one-line import."""
    checked = 0
    for index, (_, aliases) in enumerate(CORPUS):
        if index % SHARED_MODULE_EVERY != 5:
            continue
        names = sorted(name for (module, _), name in aliases.items() if module == SHARED_MODULE)
        assert len(names) == SHARED_MODELS, names
        line = f"from {SHARED_MODULE} import {', '.join(names)}"
        assert len(line) > LINE_LENGTH, line
        checked += 1
    assert checked == DESCRIPTIONS // SHARED_MODULE_EVERY, checked


@pytest.mark.parametrize("name", IMPORT_ORDER_CASES)
def test_each_import_order_case_separates_the_rule_from_a_plain_sort(name):
    """Otherwise a case would pass on a sort that does not follow its rule."""
    case = IMPORT_ORDER_CASES[name]
    assert case["plain"] != case["ruff"], case


@pytest.mark.parametrize("name", IMPORT_ORDER_CASES)
def test_each_import_order_case_emits_ruffs_order(name):
    case = IMPORT_ORDER_CASES[name]
    source = emit(_import_order_description(case))
    imports = source[: source.index("SIGNATURES")]
    positions = [
        re.search(rf"(?<![\w.]){re.escape(item)}(?![\w.])", imports) for item in case["ruff"]
    ]
    assert all(positions), (name, imports)
    starts = [match.start() for match in positions if match]
    assert starts == sorted(starts), (name, imports)


def test_every_import_block_passes_ruffs_import_sort(tmp_path):
    """The corpus and every import-order case, through one `ruff check --select I001`."""
    _write_corpus(tmp_path)
    for index, case in enumerate(IMPORT_ORDER_CASES.values()):
        (tmp_path / f"order_{index}.py").write_text(emit(_import_order_description(case)))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--no-cache",
            "--select",
            "I001",
            ".",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "All checks passed" in result.stdout, result.stdout


def test_every_generated_client_in_the_corpus_is_already_formatted(tmp_path):
    _write_corpus(tmp_path)

    result = _ruff_format_check(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{DESCRIPTIONS} files already formatted" in result.stdout, result.stdout


def test_CONTROL_annotations_written_flat_are_reformatted(tmp_path, monkeypatch):
    """The check can fail: the same corpus with every annotation left on one line."""

    def flat(node, indent, prefix, suffix, *, statement=False, comment=""):
        return [f"{indent}{prefix}{emitter._flat(node)}{suffix}{comment}"]

    monkeypatch.setattr(emitter, "_laid_out", flat)
    _write_corpus(tmp_path)

    result = _ruff_format_check(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "would be reformatted" in result.stdout

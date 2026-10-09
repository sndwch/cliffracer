"""Runtime decides what an error is from its type, not from its message text.

ADR-0011 says a check parses structure rather than relying on unanchored
substring matching. The rule here is that sentence and not a wider one: the
defect is a substring test that is the ONLY thing deciding, not a substring
test as such.

A typed check counts whether it sits in the same handler as an `isinstance`
against exception classes, or in a sibling `except` clause on the same `try`:

    except (nats.js.errors.KeyNotFoundError, nats.js.errors.KeyDeletedError):
        return default
    except Exception as exc:
        if "key not found" in str(exc).lower():
            return default
        raise

Nine of the twelve classifications in this tree are that shape, so a sweep that
walked handlers in isolation would report three quarters of them as violations.
`test_CONTROL_the_naive_handler_local_reading_is_what_this_avoids` holds that
distinction by driving both readings at the same snippet.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# Methods that decide from text rather than structure.
TEXT_METHODS = ("startswith", "endswith", "find", "index", "count")


def source_files() -> list[Path]:
    """Every shipped module: core, and each package's own source tree."""
    roots = [REPO / "src"] + sorted(p for p in (REPO / "packages").glob("*/src") if p.is_dir())
    return sorted(p for root in roots if root.is_dir() for p in root.rglob("*.py"))


def _reads_exception_text(node: ast.AST, name: str) -> bool:
    """Whether `node` is the message text of the caught exception."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("str", "repr") and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == name:
                return True
        if isinstance(func, ast.Attribute) and func.attr in ("lower", "upper", "casefold"):
            return _reads_exception_text(func.value, name)
    if isinstance(node, ast.JoinedStr):
        return any(
            _reads_exception_text(part.value, name)
            for part in node.values
            if isinstance(part, ast.FormattedValue)
        )
    return False


def _text_tests(handler: ast.ExceptHandler) -> list[tuple[int, str]]:
    """Every place this handler decides from the exception's message text."""
    name = handler.name
    if not name:
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(handler):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if not isinstance(op, ast.In | ast.NotIn):
                    continue
                if _reads_exception_text(comparator, name) or _reads_exception_text(
                    node.left, name
                ):
                    found.append((node.lineno, ast.unparse(node)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in TEXT_METHODS and _reads_exception_text(node.func.value, name):
                found.append((node.lineno, ast.unparse(node)))
    return sorted(set(found))


def _has_isinstance(handler: ast.ExceptHandler) -> bool:
    """An `isinstance` inside the handler, which is a typed check of the same decision."""
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "isinstance"
        for node in ast.walk(handler)
    )


def _names_specific_types(handler: ast.ExceptHandler) -> bool:
    """Whether this clause catches named exception types rather than everything."""
    if handler.type is None:
        return False
    caught = [handler.type]
    if isinstance(handler.type, ast.Tuple):
        caught = list(handler.type.elts)
    if isinstance(handler.type, ast.BinOp):
        caught = [handler.type.left, handler.type.right]
    return any(
        ast.unparse(node).split(".")[-1] not in ("Exception", "BaseException") for node in caught
    )


def scan(paths: list[Path] | None = None) -> tuple[list[str], list[str]]:
    """Return (decides by text alone, decides by text beside a typed check).

    The second list is what makes a green first list mean something: a sweep
    that walked nothing would report both as empty.
    """
    alone: list[str] = []
    covered: list[str] = []
    for path in paths if paths is not None else source_files():
        label = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            sibling_typed = any(_names_specific_types(h) for h in node.handlers)
            for handler in node.handlers:
                tests = _text_tests(handler)
                if not tests:
                    continue
                typed = sibling_typed or _has_isinstance(handler)
                for lineno, text in tests:
                    (covered if typed else alone).append(f"{label}:{lineno} {text[:90]}")
    return alone, covered


def test_no_runtime_path_decides_from_message_text_alone():
    alone, _ = scan()
    assert not alone, (
        "these decide what an error is from its message text with no typed check "
        "of the same decision. Catch the exception class the library raises:\n  "
        + "\n  ".join(alone)
    )


# A POSITIVE READING USED TO LIVE HERE, asserting the real tree still held a
# text test beside a typed check -- so that "no violations" could not be
# confused with "read nothing". The tree no longer holds one: the thirteen that
# remained were removed once nats-py was measured to raise every one of these
# conditions typed. The assertion said to delete it with them, and this is that.
#
# Its two jobs are both still done, and neither depends on the tree keeping a
# shape we are trying to remove. `test_the_sweep_reads_the_shipped_source`
# proves the sweep walks the tree, and
# `test_CONTROL_a_sibling_except_clause_covers_it` proves it climbs to the
# sibling clause, against a sample written in the test rather than against
# whatever source happens to survive.


def test_the_sweep_reads_the_shipped_source():
    files = source_files()
    assert len(files) > 50, f"only {len(files)} source files found; the sweep is not reading"
    assert any(p.is_relative_to(REPO / "src") for p in files), "core was not reached"
    assert any("packages" in p.parts for p in files), "no package source was reached"


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(body)
    return path


def test_CONTROL_text_alone_is_caught(tmp_path: Path):
    path = _write(
        tmp_path,
        "def f(kv, key):\n"
        "    try:\n"
        "        return kv.get(key)\n"
        "    except Exception as exc:\n"
        '        if "key not found" in str(exc).lower():\n'
        "            return None\n"
        "        raise\n",
    )
    alone, covered = scan([path])
    assert alone, "a text-only classification was not caught"
    assert not covered


def test_CONTROL_a_text_method_is_caught_too(tmp_path: Path):
    """`in` is not the only way to decide from text."""
    path = _write(
        tmp_path,
        "def f(kv, key):\n"
        "    try:\n"
        "        return kv.get(key)\n"
        "    except Exception as exc:\n"
        '        if str(exc).startswith("key not found"):\n'
        "            return None\n"
        "        raise\n",
    )
    alone, _ = scan([path])
    assert alone, "a startswith on the exception text was not caught"


def test_CONTROL_an_isinstance_in_the_same_handler_covers_it(tmp_path: Path):
    path = _write(
        tmp_path,
        "import nats\n\n"
        "def f(kv, key):\n"
        "    try:\n"
        "        return kv.get(key)\n"
        "    except Exception as exc:\n"
        "        if isinstance(exc, nats.js.errors.KeyNotFoundError) or "
        '"key not found" in str(exc).lower():\n'
        "            return None\n"
        "        raise\n",
    )
    alone, covered = scan([path])
    assert not alone, alone
    assert covered, "the covered reading missed a text test beside an isinstance"


def test_CONTROL_a_sibling_except_clause_covers_it(tmp_path: Path):
    """The shape nine of this tree's twelve classifications actually have."""
    path = _write(
        tmp_path,
        "import nats\n\n"
        "def f(kv, key):\n"
        "    try:\n"
        "        return kv.get(key)\n"
        "    except nats.js.errors.KeyNotFoundError:\n"
        "        return None\n"
        "    except Exception as exc:\n"
        '        if "key not found" in str(exc).lower():\n'
        "            return None\n"
        "        raise\n",
    )
    alone, covered = scan([path])
    assert not alone, f"a typed sibling clause was not counted as a typed check: {alone}"
    assert covered, "the covered reading missed a text test beside a typed sibling clause"


def _violations_ignoring_sibling_clauses(path: Path) -> list[str]:
    """The handler-local reading, kept only to hold the control below honest."""
    found: list[str] = []
    tree = ast.parse(path.read_text())
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
        tests = _text_tests(handler)
        if tests and not _has_isinstance(handler):
            found.extend(text for _, text in tests)
    return found


def test_CONTROL_the_naive_handler_local_reading_is_what_this_avoids(tmp_path: Path):
    """Two readings of one snippet, so the difference is pinned rather than described.

    A sweep that does not climb to the enclosing `try` calls the sibling-clause
    shape a violation. This asserts the naive reading does exactly that and the
    rule above does not, which is the whole reason the rule is written this way.
    """
    path = _write(
        tmp_path,
        "import nats\n\n"
        "def f(kv, key):\n"
        "    try:\n"
        "        return kv.get(key)\n"
        "    except nats.js.errors.KeyNotFoundError:\n"
        "        return None\n"
        "    except Exception as exc:\n"
        '        if "key not found" in str(exc).lower():\n'
        "            return None\n"
        "        raise\n",
    )
    assert _violations_ignoring_sibling_clauses(path), (
        "the naive reading no longer flags this, so the control proves nothing"
    )
    alone, _ = scan([path])
    assert not alone


def test_CONTROL_an_except_body_with_no_text_test_is_not_reported(tmp_path: Path):
    path = _write(
        tmp_path,
        "def f(kv, key):\n"
        "    try:\n"
        "        return kv.get(key)\n"
        "    except Exception:\n"
        "        return None\n",
    )
    assert scan([path]) == ([], [])

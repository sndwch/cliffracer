"""The README's environment-variable table names every variable the library and its packages read.

The README, `CLAUDE.md` and `docs/ARCHITECTURE.md` each had a list of the environment variables, and
each named one (`CLIFFRACER_LOG_DIR`) while the code read others, among them the `NATS_*` defaults
that can put a credential in a process environment. The search is the one `docs/decisions.md` states:
under `src/` and `packages/*/src/`, a call that reads the environment (`os.environ.get`,
`os.environ[...]`, `os.getenv`) with a literal name, and a settings class's `env_prefix`. The test
reads each name, or each prefix, out of the README's table.
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SOURCES = [REPO / "src", *sorted(REPO.glob("packages/*/src"))]


def _text(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def variables_read(source: str) -> set[str]:
    """Names a module reads from the environment, and the prefixes its settings classes use.

    Only the three calls and the subscript below are seen, spelled as `os.environ.get`, `os.getenv`
    and `os.environ[...]`. A read through `from os import getenv`, or through an alias of `os` or of
    `os.environ`, is not found.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            target = ast.unparse(node.func)
            if target in {"os.environ.get", "os.getenv", "environ.get"} and node.args:
                name = _text(node.args[0])
                if name:
                    found.add(name)
            for keyword in node.keywords:
                if keyword.arg == "env_prefix" and (prefix := _text(keyword.value)):
                    found.add(prefix + "*")
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            name = _text(node.slice)
            if name:
                found.add(name)
    return found


def read_by_the_code() -> dict[str, list[str]]:
    where: dict[str, list[str]] = {}
    for root in SOURCES:
        for path in sorted(root.rglob("*.py")):
            for name in variables_read(path.read_text()):
                where.setdefault(name, []).append(str(path.relative_to(REPO)))
    return where


def readme_table() -> str:
    text = (REPO / "README.md").read_text()
    start = text.index("### Environment variables")
    end = text.index("\n### ", start + 1)
    return text[start:end]


def test_the_search_finds_the_variables_it_is_meant_to_hold_the_readme_to():
    found = read_by_the_code()

    assert {"CLIFFRACER_LOG_DIR", "CLIFFRACER_SUBJECT_PREFIX", "CLIFFRACER_NATS_URL"} <= set(found)
    assert "CLIFFRACER_CYANIDE_*" in found
    assert {"NATS_URL", "NATS_CREDS", "NATS_USER", "NATS_PASSWORD", "NATS_TOKEN"} <= set(found)


def names_missing_from(table: str, read: dict[str, list[str]]) -> dict[str, list[str]]:
    """The variables in `read` the table does not name, each as a backticked name of its own.

    A name is matched whole: `NATS_URL` is not found inside `CLIFFRACER_NATS_URL`. A prefix is held
    to a backticked name that begins with it (`CLIFFRACER_CYANIDE_<FIELD>`).
    """
    named = set(re.findall(r"`([^`]+)`", table))
    return {
        name: files
        for name, files in read.items()
        if not (
            any(token.startswith(name[:-1]) for token in named)
            if name.endswith("*")
            else name in named
        )
    }


def test_every_variable_the_code_reads_is_in_the_readmes_table():
    missing = names_missing_from(readme_table(), read_by_the_code())

    assert not missing, (
        f"the code reads these and the README's environment-variable table does not name them: "
        f"{missing}. An operator reads that table to find every variable that can change what a "
        f"process does."
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('import os\nos.environ.get("A_VAR")\n', {"A_VAR"}, id="environ-get"),
        pytest.param('import os\nos.getenv("B_VAR", "x")\n', {"B_VAR"}, id="getenv"),
        pytest.param('import os\nos.environ["C_VAR"]\n', {"C_VAR"}, id="environ-subscript"),
        pytest.param(
            'C = SettingsConfigDict(env_prefix="D_PREFIX_")\n', {"D_PREFIX_*"}, id="env-prefix"
        ),
        pytest.param("import os\nos.environ.get(name)\n", set(), id="a-name-held-in-a-variable"),
        pytest.param('x = "E_VAR"\n', set(), id="a-string-that-reads-nothing"),
    ],
)
def test_CONTROL_the_reader_finds_each_way_of_reading_the_environment(source, expected):
    assert variables_read(source) == expected


def test_CONTROL_a_name_that_is_only_inside_another_name_is_reported():
    read = {"NATS_URL": ["dlq.py"], "CLIFFRACER_NATS_URL": ["generate.py"]}
    table = "| `CLIFFRACER_NATS_URL` | the generator |\n"

    assert names_missing_from(table, read) == {"NATS_URL": ["dlq.py"]}
    assert names_missing_from(table + "| `NATS_URL`, `NATS_CREDS` | dlq |\n", read) == {}


def test_CONTROL_a_variable_missing_from_the_table_is_reported():
    read = {"A_VAR": ["a.py"], "B_PREFIX_*": ["b.py"], "C_VAR": ["c.py"]}
    table = "| `A_VAR` | x |\n| `B_PREFIX_<FIELD>` | y |\n"

    assert names_missing_from(table, read) == {"C_VAR": ["c.py"]}
    assert names_missing_from(table + "| `C_VAR` | z |\n", read) == {}

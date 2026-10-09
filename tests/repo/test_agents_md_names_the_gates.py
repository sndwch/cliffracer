"""AGENTS.md lists the gate commands the pipeline actually runs.

The file tells a contributor what to run before pushing. A list written by hand
drifts the moment CI gains a step, and it drifts silently -- the doc still reads
as authoritative while omitting the gate that will red their push. So the
expected list is DERIVED FROM THE WORKFLOW rather than written here: this test
compares the document against `.gitea/workflows/ci.yml`, and there is no third
copy of the commands for the two to disagree with.

`.gitea/` and not `.github/`: Gitea reads `.gitea/workflows/` and falls back to
`.github/workflows/` only when the former is absent. Both exist in this
repository, so the `.github/` copy has never run, and a test that read it would
be checking the document against a file nothing executes.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.repo, pytest.mark.gitea_checkout]

REPO = Path(__file__).resolve().parents[2]
AGENTS = REPO / "AGENTS.md"
WORKFLOW = REPO / ".gitea" / "workflows" / "ci.yml"

# A gate is a single-line `run:` invoking the project's toolchain. The release
# job's steps are multi-line shell and are not gates a contributor runs, which
# is why the shape rather than a name list is what selects them.
GATE = re.compile(
    r"^\s+run:\s+(uv run (?:ruff|mypy|pytest|python scripts/(?:check_commit_messages|check_changelog_fragment|check_kv_compatibility|check_message_schedules)\.py).*)$"
)


def workflow_gates() -> list[str]:
    """Every gate command the pipeline runs, read from the workflow."""
    found = [
        m.group(1).strip() for line in WORKFLOW.read_text().splitlines() if (m := GATE.match(line))
    ]
    assert found, (
        f"no gate commands parsed from {WORKFLOW.relative_to(REPO)}. Either the "
        f"workflow changed shape or this pattern stopped matching it -- fix the "
        f"pattern rather than deleting the test, or the document below is checked "
        f"against nothing."
    )
    return found


def test_agents_md_exists():
    assert AGENTS.is_file(), (
        f"{AGENTS.name} is missing. It is the file a contributor reads before "
        f"their first push; the gates below are checked against it."
    )


def test_agents_md_names_every_gate_the_pipeline_runs():
    """The document and the pipeline cannot disagree about what to run."""
    text = AGENTS.read_text()
    missing = [cmd for cmd in workflow_gates() if cmd not in text]

    assert not missing, (
        f"{AGENTS.name} does not name these gate commands, which "
        f"{WORKFLOW.relative_to(REPO)} runs:\n  "
        + "\n  ".join(missing)
        + f"\n\nAdd them verbatim to {AGENTS.name}. A contributor following that "
        f"file would push without running them and find out from CI."
    )


def test_the_gate_list_is_not_trivially_satisfied():
    """A positive reading: the pattern found gates, and enough of them.

    "No missing commands" and "no commands parsed" are the same output, so the
    count is asserted rather than the absence alone.
    """
    gates = workflow_gates()
    assert len(gates) >= 5, f"only {len(gates)} gate(s) parsed: {gates}"
    # Named rather than positional: `cmd.split()[2]` assumes the tool is the
    # third token, which holds for `uv run <tool>` and would quietly stop
    # contributing a kind -- rather than fail -- for a gate spelled differently.
    kinds = {
        tool for tool in ("ruff", "mypy", "pytest") if any(tool in cmd.split() for cmd in gates)
    }
    assert {"ruff", "mypy", "pytest"} == kinds, (
        f"expected a gate for each of ruff, mypy and pytest; found {sorted(kinds)} in {gates}"
    )


def test_CONTROL_a_document_missing_a_gate_is_reported(tmp_path: Path):
    """The offence, since the real document currently has none."""
    doc = tmp_path / "AGENTS.md"
    gates = workflow_gates()
    doc.write_text("\n".join(gates[:-1]))

    missing = [cmd for cmd in gates if cmd not in doc.read_text()]

    assert missing == [gates[-1]], (
        f"dropping one gate from the document should report exactly it; got {missing}"
    )


def test_CONTROL_a_document_naming_all_of_them_is_not_reported(tmp_path: Path):
    """And the rule is not "always report", which the control above would satisfy."""
    doc = tmp_path / "AGENTS.md"
    doc.write_text("\n".join(workflow_gates()))

    assert [cmd for cmd in workflow_gates() if cmd not in doc.read_text()] == []

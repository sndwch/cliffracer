"""`$CLIFFRACER_SUBJECT_PREFIX` is held to the same rule as an explicit `subject_prefix`.

Pydantic does not validate a default, so the field validator never ran for the value read from the
environment: `subject_prefix="a.b"` was refused, and the same text from the environment built a
config whose stream names (`a.b_ORDERS`) the broker then refused at start, after the connection
was made. The field validates its default now, and the refusal names the environment variable.

The config table generator documents each field's default, and read the default of this one from
the environment of whoever ran it, so a shell that exports the prefix made `--check` report the
table stale and would have written the value into the documented default.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from cliffracer import ServiceConfig

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
VARIABLE = "CLIFFRACER_SUBJECT_PREFIX"


def build(**kwargs) -> ServiceConfig:
    return ServiceConfig(name="orders", health_port=0, **kwargs)


@pytest.mark.parametrize("value", ["a.b", "my-env", "x y", "a>", "a*", "é-"])
def test_an_invalid_prefix_from_the_environment_is_refused_when_the_config_is_built(
    monkeypatch, value
):
    monkeypatch.setenv(VARIABLE, value)

    with pytest.raises(ValidationError) as caught:
        build()

    assert VARIABLE in str(caught.value), str(caught.value)
    assert repr(value) in str(caught.value), str(caught.value)


@pytest.mark.parametrize("value", ["a.b", "my-env"])
def test_the_same_text_given_explicitly_is_still_refused(monkeypatch, value):
    monkeypatch.delenv(VARIABLE, raising=False)

    with pytest.raises(ValidationError) as caught:
        build(subject_prefix=value)

    assert VARIABLE not in str(caught.value), str(caught.value)


def test_CONTROL_a_valid_prefix_from_the_environment_is_used(monkeypatch):
    monkeypatch.setenv(VARIABLE, "staging_1")

    assert build().subject_prefix == "staging_1"


def test_CONTROL_an_empty_or_absent_variable_means_no_prefix(monkeypatch):
    monkeypatch.setenv(VARIABLE, "")
    assert build().subject_prefix is None
    monkeypatch.delenv(VARIABLE)
    assert build().subject_prefix is None


def test_CONTROL_an_explicit_prefix_wins_over_an_invalid_environment_value(monkeypatch):
    monkeypatch.setenv(VARIABLE, "a.b")

    assert build(subject_prefix="prod").subject_prefix == "prod"
    assert build(subject_prefix=None).subject_prefix is None


def run_generator(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("CLIFFRACER_")}
    child_env.update(env)
    return subprocess.run(
        [sys.executable, str(REPO / "tools" / "gen_service_config_table.py"), *args],
        cwd=REPO,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_table_generator_documents_the_default_whatever_the_shell_exports():
    quiet = run_generator("--check")
    exporting = run_generator("--check", **{VARIABLE: "staging"})

    assert quiet.returncode == 0, quiet.stdout + quiet.stderr
    assert exporting.returncode == 0, exporting.stdout + exporting.stderr

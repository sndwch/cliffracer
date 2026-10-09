"""A refused `describe` earns the `--header` hint by its `code`, not by what its text starts with.

ADR-0011: classification reads a reply's `code`, and its message text only for a reply that has none
(an old service). The command decided on `reason.startswith("refused: ")`, so a crash whose own text
began that way was told to send credentials, and a refusal worded differently was not.
"""

import json

import pytest

from cliffracer.generate_client.cli import main

pytestmark = pytest.mark.unit

HINT = "--header authorization="


def _run(monkeypatch, tmp_path, capsys, reply: dict) -> str:
    async def fake_fetch(*args, **kwargs):
        return json.dumps(reply).encode()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fake_fetch)
    code = main(["--service", "x", "--out", str(tmp_path / "c.py")])
    assert code == 4
    return capsys.readouterr().err


def test_a_coded_refusal_gets_the_hint_whatever_its_wording(monkeypatch, tmp_path, capsys):
    err = _run(
        monkeypatch,
        tmp_path,
        capsys,
        {"success": False, "code": "refused", "error": "unauthenticated"},
    )

    assert HINT in err, err
    assert "unauthenticated" in err, err


def test_a_coded_server_fault_whose_text_reads_like_a_refusal_gets_no_hint(
    monkeypatch, tmp_path, capsys
):
    err = _run(
        monkeypatch,
        tmp_path,
        capsys,
        {"success": False, "code": "internal", "error": "refused: the handler's own message"},
    )

    assert HINT not in err, err
    assert "refused: the handler's own message" in err, "the reason is still shown"


def test_an_old_service_with_no_code_is_still_read_by_its_prose(monkeypatch, tmp_path, capsys):
    err = _run(monkeypatch, tmp_path, capsys, {"error": "refused: no token"})

    assert HINT in err, err


def test_an_old_service_error_that_is_not_a_refusal_gets_no_hint(monkeypatch, tmp_path, capsys):
    err = _run(
        monkeypatch, tmp_path, capsys, {"error": "Internal server error (correlation_id: c)"}
    )

    assert HINT not in err, err
    assert "Internal server error" in err

"""Tests verifying exit codes and URL resolution of cliffracer-generate-client."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import nats.errors
import pytest

import cliffracer
from cliffracer.generate_client.cli import describe_subject, main, resolve_nats_url
from conftest import console_script

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

#: The `src` directory this process imported `cliffracer` from. A child is pointed at it, so it runs
#: the code under test: without it the child imports whatever the interpreter has installed, which
#: in a scratch copy of the tree is another tree's code.
IMPORTED_SRC = str(Path(cliffracer.__file__).resolve().parents[1])


def test_url_precedence_flag_env_default():
    assert resolve_nats_url("nats://a:1", {"CLIFFRACER_NATS_URL": "nats://b:2"}) == "nats://a:1"
    assert resolve_nats_url(None, {"CLIFFRACER_NATS_URL": "nats://b:2"}) == "nats://b:2"
    assert resolve_nats_url(None, {}) == "nats://localhost:4222"


def test_class_mode_writes_a_client(tmp_path):
    out = tmp_path / "c.py"
    rc = main(
        [
            "--class",
            "tests.unit.test_introspect:Orders",
            "--service",
            "orders",
            "--version",
            "1",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    assert "class OrdersClient(ServiceClient)" in out.read_text()


def test_exit_5_when_the_class_cannot_be_imported(capsys):
    rc = main(["--class", "no.such:Thing", "--service", "x"])
    assert rc == 5
    assert "no.such" in capsys.readouterr().err


def test_exit_3_when_no_broker(capsys):
    rc = main(["--service", "x", "--nats-url", "nats://127.0.0.1:1", "--timeout", "0.5"])
    assert rc == 3
    assert "nats://127.0.0.1:1" in capsys.readouterr().err


def test_exit_4_via_class_mode_for_an_unemittable_type(capsys, tmp_path):
    """A model a generated client could not import. The service runs fine; only
    the generator refuses, which is why this is the command's rule and not a
    refuse-to-start one."""
    mod = tmp_path / "svc_mod.py"
    mod.write_text(
        "from pydantic import BaseModel\n"
        "from cliffracer import CliffracerService, rpc\n"
        "class X(BaseModel):\n"
        "    a: int\n"
        "X.__module__ = '__main__'\n"
        "class S(CliffracerService):\n"
        "    @rpc\n"
        "    async def f(self, x: X) -> int:\n"
        "        return 1\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        rc = main(["--class", "svc_mod:S", "--service", "s"])
    finally:
        sys.path.remove(str(tmp_path))
    assert rc == 4
    assert "__main__" in capsys.readouterr().err


def test_exit_4_when_a_handler_is_not_annotated(capsys, tmp_path):
    """The other way to be undescribable, and the one a service author hits."""
    mod = tmp_path / "untyped_mod.py"
    mod.write_text(
        "from cliffracer import CliffracerService, rpc\n"
        "class S(CliffracerService):\n"
        "    @rpc\n"
        "    async def f(self, x):\n"
        "        return x\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        rc = main(["--class", "untyped_mod:S", "--service", "s"])
    finally:
        sys.path.remove(str(tmp_path))
    assert rc == 4
    err = capsys.readouterr().err
    assert "S.f" in err and "x" in err


def test_the_console_script_is_installed():
    exe = console_script("cliffracer-generate-client")
    assert subprocess.run([exe, "--help"], capture_output=True).returncode == 0


UNTYPED_SERVICE = (
    "from cliffracer import CliffracerService, rpc\n"
    "class S(CliffracerService):\n"
    "    @rpc\n"
    "    async def f(self, x):\n"
    "        return x\n"
)
NO_RPC_SERVICE = (
    "from cliffracer import CliffracerService\n"
    "class S(CliffracerService):\n"
    "    async def helper(self) -> int:\n"
    "        return 1\n"
)


def _live(monkeypatch, reply=None, *, raises=None):
    """Make the live-mode fetch answer `reply` (or raise), and return the argv that reaches it."""

    async def fake_fetch(*args, **kwargs):
        if raises is not None:
            raise raises
        return reply

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fake_fetch)
    return ["--service", "x"]


def _empty_description_reply() -> bytes:
    from cliffracer import CliffracerService
    from cliffracer.introspect import describe

    class Empty(CliffracerService):
        pass

    return json.dumps(describe(Empty, service="x", version="1").to_dict()).encode()


def _a_class_on_the_path(tmp_path, monkeypatch, name, source):
    (tmp_path / f"{name}.py").write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    return f"{name}:S"


# Every way the command can fail before it has a client to write, each given
# `--out` so that a write on that path would have somewhere to land. The module
# docstring promises all of them: "every failure path leaves no file behind".
_WARE = "tests.fixtures.typed_client.service:Warehouse"
FAILURES = [
    pytest.param(5, lambda mp, tp: ["--class", "no.such:Thing", "--service", "x"], id="5-no-class"),
    pytest.param(
        4,
        lambda mp, tp: [
            "--class",
            "tests.fixtures.typed_client.service_generic:GenericService",
            "--service",
            "generic-service",
        ],
        id="4-unimportable-model",
    ),
    pytest.param(
        4, lambda mp, tp: ["--class", _WARE, "--service", "order.service"], id="4-cannot-emit"
    ),
    pytest.param(
        4,
        lambda mp, tp: [
            "--class",
            _a_class_on_the_path(tp, mp, "untyped_for_out", UNTYPED_SERVICE),
            "--service",
            "s",
        ],
        id="4-untyped-handler",
    ),
    pytest.param(
        4,
        lambda mp, tp: [
            "--class",
            _a_class_on_the_path(tp, mp, "no_rpc_for_out", NO_RPC_SERVICE),
            "--service",
            "s",
        ],
        id="4-class-with-no-rpc",
    ),
    pytest.param(
        3,
        lambda mp, tp: _live(mp, raises=ConnectionRefusedError("refused")),
        id="3-no-broker",
    ),
    pytest.param(
        2,
        lambda mp, tp: _live(mp, raises=nats.errors.NoRespondersError()),
        id="2-no-responders",
    ),
    pytest.param(
        4,
        lambda mp, tp: _live(mp, b"<html>not json</html>"),
        id="4-reply-is-not-json",
    ),
    pytest.param(
        4,
        lambda mp, tp: _live(mp, b'{"error": "refused: no token"}'),
        id="4-description-refused",
    ),
    pytest.param(
        4,
        lambda mp, tp: _live(mp, _empty_description_reply()),
        id="4-live-service-with-no-rpc",
    ),
]


@pytest.mark.parametrize(("expected_rc", "argv_for"), FAILURES)
def test_never_writes_a_partial_file(expected_rc, argv_for, tmp_path, monkeypatch, capsys):
    """A half-written client is worse than none: it imports, and it lies.

    Held across every failure the command has before the write, and with and
    without a previous client at `--out`: a failure must neither create a file
    nor touch the one that was there.
    """
    out = tmp_path / "c.py"
    argv = [*argv_for(monkeypatch, tmp_path), "--out", str(out)]

    assert main(argv) == expected_rc, capsys.readouterr().err
    assert not out.exists()

    out.write_text("# the previous client\n")
    assert main(argv) == expected_rc
    assert out.read_text() == "# the previous client\n"


def test_exit_4_when_service_name_is_invalid(capsys, tmp_path):
    # Invalid service name on CLI returns exit code 4.
    ret = main(
        [
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "order.service",
            "--out",
            str(tmp_path / "out.py"),
        ]
    )
    assert ret == 4
    _, err = capsys.readouterr()
    assert "order.service" in err
    assert not (tmp_path / "out.py").exists()


def test_exit_4_when_model_is_parametrized_generic(capsys, tmp_path):
    # Parametrized generic model returns exit code 4.
    ret = main(
        [
            "--class",
            "tests.fixtures.typed_client.service_generic:GenericService",
            "--service",
            "generic-service",
            "--out",
            str(tmp_path / "out.py"),
        ]
    )
    assert ret == 4
    _, err = capsys.readouterr()
    assert "Page[int]" in err
    assert "Move these models into an importable package" in err
    assert not (tmp_path / "out.py").exists()


def test_exit_2_when_service_request_times_out(monkeypatch, capsys):
    """Verify fetch_description timeout returns exit code 2."""
    import nats.errors

    async def mock_fetch(*args, **kwargs):
        raise nats.errors.TimeoutError()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", mock_fetch)
    rc = main(["--service", "slow_svc", "--nats-url", "nats://127.0.0.1:4222", "--timeout", "1.0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "slow_svc" in err
    assert "answered on the describe subject within 1.0s" in err


def test_exit_2_when_no_responders(monkeypatch, capsys):
    """Verify fetch_description NoRespondersError returns exit code 2."""
    import nats.errors

    async def mock_fetch(*args, **kwargs):
        raise nats.errors.NoRespondersError()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", mock_fetch)
    rc = main(["--service", "nobody", "--nats-url", "nats://127.0.0.1:4222", "--timeout", "1.0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "nobody" in err
    assert "answered on the describe subject within 1.0s" in err


# --- a failed --out write leaves the previous client alone --------------------
#
# `test_never_writes_a_partial_file` above covers a failure BEFORE the write:
# the class cannot be imported, so nothing is generated. It cannot see a
# failure DURING the write, which is where the damage was -- `write_text` opens
# the target and truncates it before encoding a byte, so an encoding failure or
# a full disk left a 0-byte file where a working client used to be.

ACCENTED_SERVICE = (
    "from cliffracer import CliffracerService, rpc\n"
    "\n"
    "\n"
    "class Accented(CliffracerService):\n"
    "    @rpc\n"
    "    async def go(self, x: str) -> str:\n"
    '        """Résumé the run and return its identifiant."""\n'
    "        return x\n"
)


def _accented_on_path(tmp_path, monkeypatch):
    (tmp_path / "accsvc.py").write_text(ACCENTED_SERVICE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return "accsvc:Accented"


def test_exit_6_when_the_target_directory_does_not_exist(capsys, tmp_path):
    out = tmp_path / "nosuchdir" / "c.py"

    ret = main(
        [
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "warehouse",
            "--out",
            str(out),
        ]
    )

    assert ret == 6
    _, err = capsys.readouterr()
    assert str(out) in err, err
    assert "No such file or directory" in err, err
    # the temporary name is never the reader's business
    assert ".tmp" not in err, err
    assert not out.exists()


def test_exit_6_when_the_target_is_a_directory(capsys, tmp_path):
    ret = main(
        [
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "warehouse",
            "--out",
            str(tmp_path),
        ]
    )

    assert ret == 6
    _, err = capsys.readouterr()
    assert "Is a directory" in err, err


def test_a_failed_write_leaves_the_previous_client_intact(capsys, monkeypatch, tmp_path):
    """The defect, driven: the failure has to happen DURING the write.

    `os.replace` is made to fail, which is the last step and the only one that
    can fail after the new source is already on disk. Before the fix the target
    was truncated at `open`, so there was no step at which the previous client
    still existed.
    """
    from cliffracer.generate_client import cli

    out = tmp_path / "c.py"
    out.write_text("PREVIOUS CLIENT THAT MUST SURVIVE\n")

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli.os, "replace", boom)

    ret = main(
        [
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "warehouse",
            "--out",
            str(out),
        ]
    )

    assert ret == 6
    assert out.read_text() == "PREVIOUS CLIENT THAT MUST SURVIVE\n"
    _, err = capsys.readouterr()
    assert "No space left on device" in err, err


def test_no_temporary_file_is_left_behind_by_a_failed_write(monkeypatch, tmp_path):
    from cliffracer.generate_client import cli

    out = tmp_path / "c.py"
    out.write_text("PREVIOUS\n")
    monkeypatch.setattr(
        cli.os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError(28, "nope"))
    )

    main(
        [
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "warehouse",
            "--out",
            str(out),
        ]
    )

    leftovers = [entry.name for entry in tmp_path.iterdir() if entry.name != "c.py"]
    assert leftovers == [], leftovers


def test_CONTROL_the_replace_failure_is_reachable(monkeypatch, tmp_path):
    """Without the mutation the same invocation succeeds, so the two tests above
    are observing the failure they inject and not an unrelated one."""
    out = tmp_path / "c.py"
    out.write_text("PREVIOUS\n")

    ret = main(
        [
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "warehouse",
            "--out",
            str(out),
        ]
    )

    assert ret == 0
    assert out.read_text() != "PREVIOUS\n"


def test_the_client_is_written_as_utf8_whatever_the_locale(tmp_path, monkeypatch):
    """A handler docstring is the only place a non-ASCII character survives.

    `_literal` routes every string through `json.dumps`, which escapes them, so
    the docstring is the whole exposure -- and it reaches the file verbatim.
    """
    target = _accented_on_path(tmp_path, monkeypatch)
    out = tmp_path / "c.py"

    ret = main(["--class", target, "--service", "accented", "--out", str(out)])

    assert ret == 0
    raw = out.read_bytes()
    assert "Résumé" in raw.decode("utf-8"), raw[:200]
    # the control on the control: a pure-ASCII file would pass the decode above
    assert not raw.isascii(), "this test is only meaningful while the source is non-ASCII"


def test_the_console_script_writes_utf8_under_a_c_locale(tmp_path):
    """End to end, in a subprocess, under the locale that produced the defect.

    In-process the explicit `encoding="utf-8"` makes the locale irrelevant --
    which is the fix -- so the only way to show the locale no longer decides is
    to set one. PEP 538 coercion is disabled because this machine would
    otherwise coerce `C` to a UTF-8 locale and the run would prove nothing.
    """
    (tmp_path / "accsvc.py").write_text(ACCENTED_SERVICE, encoding="utf-8")
    out = tmp_path / "c.py"

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "cliffracer.generate_client.cli",
            "--class",
            "accsvc:Accented",
            "--service",
            "accented",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        env=_c_locale_env(tmp_path),
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "Résumé" in out.read_bytes().decode("utf-8")


def _c_locale_env(service_dir: Path) -> dict[str, str]:
    """The C-locale child's environment: the service's directory and the imported `src` on its
    path, the locale `C`, and neither PEP 538 coercion nor UTF-8 mode to undo it."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join([str(service_dir), IMPORTED_SRC]),
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
    }


def test_the_c_locale_child_imports_the_code_this_process_imported(tmp_path):
    """The console-script row reads the code under test only if its child imports it."""
    done = subprocess.run(
        [sys.executable, "-c", "import cliffracer, sys; sys.stdout.write(cliffracer.__file__)"],
        capture_output=True,
        text=True,
        env=_c_locale_env(tmp_path),
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert Path(done.stdout).resolve().parents[1] == Path(IMPORTED_SRC), (done.stdout, IMPORTED_SRC)


def test_an_existing_client_keeps_the_mode_it_had(tmp_path):
    """Replacing a file carries the replacement's mode onto it.

    `tempfile` creates at 0600 so a secret cannot be read before it is used,
    which is right for a secret and wrong for generated source: without an
    explicit mode every regenerated client would silently become owner-only.
    """
    out = tmp_path / "c.py"
    out.write_text("PREVIOUS\n")
    out.chmod(0o664)

    main(
        [
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "warehouse",
            "--out",
            str(out),
        ]
    )

    assert stat.S_IMODE(out.stat().st_mode) == 0o664


def test_a_new_client_is_not_written_owner_only(tmp_path):
    """And a target that did not exist gets the mode `open` would have chosen,
    not `tempfile`'s 0600."""
    out = tmp_path / "fresh.py"

    main(
        [
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "warehouse",
            "--out",
            str(out),
        ]
    )

    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode != 0o600, oct(mode)
    assert mode & 0o044, f"neither group nor other can read it: {oct(mode)}"


def test_exit_6_is_documented_where_the_interface_is_stated():
    """The module docstring calls the exit codes the interface, and the
    reference table repeats them. A code that only one of them knows about is
    an undocumented interface either way."""
    from cliffracer.generate_client import cli

    reference = (REPO / "docs" / "api-reference.md").read_text()

    assert "6  the client could not be written" in (cli.__doc__ or "")
    assert "| 6 |" in reference, "exit 6 is missing from the api-reference table"


def test_the_temporary_file_is_created_beside_the_target(monkeypatch, tmp_path):
    """`os.replace` is atomic only within one filesystem.

    A temporary file in the system temp directory would make the final step a
    copy across devices -- `OSError: Invalid cross-device link` when it fails
    outright, and a non-atomic copy when it does not, which is the defect again
    with more steps. Asserted by looking at what is beside the target at the
    moment of the replace, rather than by reading the `dir=` argument: the
    argument is the decision, the file's location is the consequence.

    Not reachable as a failure in this suite -- `tmp_path` and the system temp
    directory are one filesystem here, so a misplaced temporary file still
    replaces cleanly and every other test stays green. Measured: with
    `dir=None` the whole file passed.
    """
    from cliffracer.generate_client import cli

    out = tmp_path / "c.py"
    seen: list[list[str]] = []
    real_replace = cli.os.replace

    def watching_replace(src, dst):
        seen.append(sorted(entry.name for entry in Path(dst).parent.iterdir()))
        return real_replace(src, dst)

    monkeypatch.setattr(cli.os, "replace", watching_replace)

    ret = main(
        [
            "--class",
            "tests.fixtures.typed_client.service:Warehouse",
            "--service",
            "warehouse",
            "--out",
            str(out),
        ]
    )

    assert ret == 0
    assert seen, "os.replace was never reached, so this test measured nothing"
    beside = [name for name in seen[0] if name.endswith(".tmp")]
    assert beside, f"no temporary file beside the target at replace time: {seen[0]}"


# --- a target with nothing to call is refused, not written ------------------
#
# `describe` walks whatever it is handed and publishes the public @rpc
# handlers it finds. Handed a function, a dict, a class with no handlers, or a
# class whose only handler is underscored, it finds none and returns a valid
# description with zero methods -- which emitted a client with nothing to call
# and exited 0. A zero-method client is never what anyone meant to generate, so
# both the in-process and the live path refuse it, and neither writes a file.

SHAPES_MODULE = (
    "from cliffracer import CliffracerService, rpc\n"
    "CONFIG = {'a': 1}\n"
    "def a_function():\n"
    "    return None\n"
    "class Plain:\n"
    "    pass\n"
    "class NoHandlers(CliffracerService):\n"
    "    pass\n"
    "class OnlyUnderscored(CliffracerService):\n"
    "    @rpc\n"
    "    async def _hidden(self, x: int) -> int:\n"
    "        return x\n"
)


@pytest.fixture
def shapes_on_path(tmp_path, monkeypatch):
    (tmp_path / "target_shapes.py").write_text(SHAPES_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "target_shapes", raising=False)
    return tmp_path


@pytest.mark.parametrize(
    ("target", "kind"),
    [
        ("json:dumps", "function"),
        ("target_shapes:a_function", "function"),
        ("target_shapes:CONFIG", "dict"),
    ],
)
def test_exit_5_when_the_target_is_not_a_class(capsys, shapes_on_path, target, kind):
    out = shapes_on_path / "c.py"

    rc = main(["--class", target, "--service", "orders", "--out", str(out)])

    assert rc == 5
    assert f"{target} is a {kind}, not a class" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("cls", ["Plain", "NoHandlers"])
def test_exit_4_when_the_class_has_no_rpc_handler(capsys, shapes_on_path, cls):
    out = shapes_on_path / "c.py"

    rc = main(["--class", f"target_shapes:{cls}", "--service", "orders", "--out", str(out)])

    assert rc == 4
    assert f"no @rpc handler found on target_shapes:{cls}" in capsys.readouterr().err
    assert not out.exists()


def test_exit_4_naming_the_refusal_when_the_only_handler_is_on_an_underscored_name(
    capsys, shapes_on_path
):
    """The service would refuse to start, so the generator says why and not that it found none."""
    out = shapes_on_path / "c.py"

    rc = main(
        ["--class", "target_shapes:OnlyUnderscored", "--service", "orders", "--out", str(out)]
    )

    err = capsys.readouterr().err
    assert rc == 4
    assert "OnlyUnderscored._hidden is decorated with @rpc or @async_rpc" in err, err
    assert "starts with an underscore" in err and not out.exists()


def _live_description(methods):
    return {
        "service": "orders",
        "version": "1",
        "description_hash": "sha256:d",
        "methods": methods,
    }


GO_METHOD = {
    "name": "go",
    "signature_hash": "sha256:s",
    "doc": None,
    "params": [],
    "returns": {"kind": "scalar", "name": "str"},
}


def test_exit_4_when_the_live_service_describes_no_rpc_method(monkeypatch, capsys, tmp_path):
    async def fetch(*args, **kwargs):
        return json.dumps(_live_description([])).encode()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)
    out = tmp_path / "c.py"

    rc = main(["--service", "orders", "--out", str(out)])

    assert rc == 4
    assert "orders answered describe with no @rpc handler" in capsys.readouterr().err
    assert not out.exists()


def test_CONTROL_a_live_service_with_one_rpc_method_is_written(monkeypatch, tmp_path):
    """The live refusal is keyed on zero methods, not on the live path.

    `test_class_mode_writes_a_client` is the same control for `--class`.
    """

    async def fetch(*args, **kwargs):
        return json.dumps(_live_description([GO_METHOD])).encode()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)
    out = tmp_path / "c.py"

    rc = main(["--service", "orders", "--out", str(out)])

    assert rc == 0
    assert "async def go(self) -> str:" in out.read_text()


def test_the_refusals_are_documented_where_the_interface_is_stated():
    from cliffracer.generate_client import cli

    reference = (REPO / "docs" / "api-reference.md").read_text()
    readme = (REPO / "README.md").read_text()

    assert "5  the class named by --class could not be imported, or is not a class" in (
        cli.__doc__ or ""
    )
    for name, text in (("docs/api-reference.md", reference), ("README.md", readme)):
        assert "| 4 | the service cannot be described, has no rpc handler," in text, name
        assert "| 5 | the class named by `--class` could not be imported, or is not a class |" in (
            text
        ), name
        assert "| 6 | the client could not be written where `--out` asked |" in text, name
        assert "| 7 | the command line is wrong" in text, name
    assert "7  the command line is wrong" in (cli.__doc__ or "")


# --- the version --class records ---------------------------------------------
#
# A class cannot see the ServiceConfig it is started with, and that config is
# where a running service's version comes from. `--version` defaulted to "0",
# which is truthy, so `describe`'s fallback never ran and every class-mode
# client recorded version 0. Without the flag the class-mode client now records
# what an unconfigured service reports, and live mode, where the service
# reports its own, refuses the flag rather than ignoring it.

WAREHOUSE = "tests.fixtures.typed_client.service:Warehouse"


def _recorded_version(path: Path) -> str:
    (line,) = [ln for ln in path.read_text().splitlines() if ln.strip().startswith("VERSION = ")]
    return line.split("=", 1)[1].strip().strip('"')


def test_class_mode_without_version_records_what_an_unconfigured_service_reports(tmp_path):
    from cliffracer import ServiceConfig

    out = tmp_path / "c.py"

    assert main(["--class", WAREHOUSE, "--service", "warehouse", "--out", str(out)]) == 0

    assert _recorded_version(out) == ServiceConfig(name="warehouse").version


def test_class_mode_records_the_version_it_is_given(tmp_path):
    out = tmp_path / "c.py"

    rc = main(
        ["--class", WAREHOUSE, "--service", "warehouse", "--version", "3.1.4", "--out", str(out)]
    )

    assert rc == 0
    assert _recorded_version(out) == "3.1.4"


def test_describe_falls_back_to_the_service_config_default():
    """The same source of truth for a direct caller of `describe`."""
    from cliffracer import ServiceConfig
    from cliffracer.introspect import describe
    from tests.fixtures.typed_client.service import Warehouse

    assert describe(Warehouse, service="warehouse").version == ServiceConfig(name="w").version


def test_live_mode_refuses_version(monkeypatch, capsys, tmp_path):
    async def fetch(*args, **kwargs):
        raise AssertionError("a refused invocation must not ask the broker")

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)
    out = tmp_path / "c.py"

    with pytest.raises(SystemExit) as exited:
        main(["--service", "orders", "--version", "9.9", "--out", str(out)])

    assert exited.value.code == 7

    assert "--version applies only with --class" in capsys.readouterr().err
    assert not out.exists()


# --- a usage error has its own exit code -------------------------------------
#
# argparse exits 2 for every usage error, and 2 is the code this command
# documents for "the broker answered and no such service did". A malformed
# `--header` exited 5, the code for "--class could not be imported". A script
# branching on the code was sent to the wrong place either way. Every mistake
# in the command line -- including a flag the chosen mode does not use --
# now exits 7 and writes nothing.


def _usage_exit(capsys, argv):
    with pytest.raises(SystemExit) as exited:
        main(argv)
    return exited.value.code, capsys.readouterr().err


USAGE_ERRORS = [
    ("missing_service", ["--class", WAREHOUSE], "the following arguments are required: --service"),
    (
        "unknown_flag",
        ["--service", "orders", "--colour", "red"],
        "unrecognized arguments: --colour",
    ),
    ("timeout_not_a_number", ["--service", "orders", "--timeout", "soon"], "--timeout"),
    (
        "header_without_equals",
        ["--service", "orders", "--header", "not-a-pair"],
        "--header wants NAME=VALUE, got 'not-a-pair'",
    ),
    (
        "header_without_name",
        ["--service", "orders", "--header", "=value"],
        "--header wants NAME=VALUE, got '=value'",
    ),
    (
        "header_blank_name",
        ["--service", "orders", "--header", " =v"],
        "--header wants NAME=VALUE, got ' =v'",
    ),
    (
        "version_without_class",
        ["--service", "orders", "--version", "9"],
        "--version applies only with --class",
    ),
    (
        "header_with_class",
        ["--class", WAREHOUSE, "--service", "w", "--header", "a=b"],
        "--header applies only without --class",
    ),
    (
        "nats_url_with_class",
        ["--class", WAREHOUSE, "--service", "w", "--nats-url", "nats://h:1"],
        "--nats-url applies only without --class",
    ),
    (
        "timeout_with_class",
        ["--class", WAREHOUSE, "--service", "w", "--timeout", "1"],
        "--timeout applies only without --class",
    ),
]


@pytest.mark.parametrize(
    ("argv", "says"), [c[1:] for c in USAGE_ERRORS], ids=[c[0] for c in USAGE_ERRORS]
)
def test_a_usage_error_exits_7(monkeypatch, capsys, tmp_path, argv, says):
    async def fetch(*args, **kwargs):
        raise AssertionError("a usage error must not ask the broker")

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)
    out = tmp_path / "c.py"

    code, err = _usage_exit(capsys, [*argv, "--out", str(out)])

    assert code == 7, err
    assert says in err
    assert not out.exists()


def test_CONTROL_help_still_exits_0(capsys):
    code, _ = _usage_exit(capsys, ["--help"])

    assert code == 0


def test_CONTROL_a_header_is_still_sent(monkeypatch, tmp_path):
    sent: list[dict[str, str] | None] = []

    async def fetch(url, service, namespace, timeout, headers=None):
        sent.append(headers)
        return json.dumps(_live_description([GO_METHOD])).encode()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)
    argv = ["--service", "orders", "--header", "authorization=bearer a=b", "--header", "x=1"]

    assert main([*argv, "--out", str(tmp_path / "c.py")]) == 0
    assert sent == [{"authorization": "bearer a=b", "x": "1"}]


# --- a generated client records its namespace --------------------------------
#
# `--namespace` chose only the subject the generator asked `describe` on. The
# client it wrote recorded nothing about it, and `ServiceClient` takes its
# namespace only from the constructor, so a client generated against `prod`
# called the un-namespaced subjects unless every caller repeated
# `namespace="prod"`. The client now records the namespace as `NAMESPACE`, and
# a constructor given none uses it.


def _live_warehouse(monkeypatch):
    from cliffracer.introspect import canonical, describe
    from tests.fixtures.typed_client.service import Warehouse

    body = canonical(describe(Warehouse, service="warehouse", version="3.1.4").to_dict())

    async def fetch(*args, **kwargs):
        return body.encode()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)


def _load_client(path: Path, name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.WarehouseClient


def test_a_client_generated_with_a_namespace_calls_inside_it(monkeypatch, tmp_path):
    _live_warehouse(monkeypatch)
    out = tmp_path / "ns_client.py"

    assert main(["--service", "warehouse", "--namespace", "prod", "--out", str(out)]) == 0

    client = _load_client(out, "ns_client")(verify=False)
    assert client.namespace == "prod"
    assert client._subject("rpc.reserve") == "prod.warehouse.rpc.reserve"


def test_class_mode_records_the_namespace_it_is_given_and_matches_live(monkeypatch, tmp_path):
    _live_warehouse(monkeypatch)
    live, from_class = tmp_path / "live.py", tmp_path / "cls.py"

    assert main(["--service", "warehouse", "--namespace", "prod", "--out", str(live)]) == 0
    argv = ["--class", WAREHOUSE, "--service", "warehouse", "--version", "3.1.4"]
    assert main([*argv, "--namespace", "prod", "--out", str(from_class)]) == 0

    assert 'NAMESPACE = "prod"' in from_class.read_text()
    assert live.read_text() == from_class.read_text()


def test_a_constructor_namespace_still_wins_and_empty_means_none(monkeypatch, tmp_path):
    """The escape hatch for a client generated with a namespace: `None` now
    means "use the recorded one", and `""` means no namespace, because the
    subject helper scopes only a non-empty namespace."""
    _live_warehouse(monkeypatch)
    out = tmp_path / "ns_client2.py"
    assert main(["--service", "warehouse", "--namespace", "prod", "--out", str(out)]) == 0
    client_class = _load_client(out, "ns_client2")

    assert client_class(namespace="staging", verify=False)._subject("rpc.x") == (
        "staging.warehouse.rpc.x"
    )
    assert client_class(namespace="", verify=False)._subject("rpc.x") == "warehouse.rpc.x"


def test_CONTROL_without_a_namespace_nothing_is_recorded(monkeypatch, tmp_path):
    _live_warehouse(monkeypatch)
    out = tmp_path / "plain_client.py"

    assert main(["--service", "warehouse", "--out", str(out)]) == 0

    assert "NAMESPACE" not in out.read_text()
    client = _load_client(out, "plain_client")(verify=False)
    assert client._subject("rpc.x") == "warehouse.rpc.x"


@pytest.mark.parametrize("namespace", ["a.b", "a b", "*", ">"])
def test_a_namespace_no_service_could_have_exits_7(capsys, tmp_path, namespace):
    out = tmp_path / "c.py"

    code, err = _usage_exit(
        capsys,
        ["--class", WAREHOUSE, "--service", "w", "--namespace", namespace, "--out", str(out)],
    )

    assert code == 7, err
    assert "--namespace" in err and "single subject token" in err
    assert not out.exists()


@pytest.mark.parametrize("prefix", ["a.b", "my-env", "x y"])
def test_a_bad_subject_prefix_in_the_environment_is_refused_by_name_before_a_service_is_asked(
    monkeypatch, capsys, tmp_path, prefix
):
    """A running service is asked on a subject starting with the environment's prefix.

    A prefix no service can serve builds a subject nothing answers, so the command waited out
    `--timeout` and blamed `--service` and `--namespace`. It is refused at once, naming the variable.
    """
    asked = []

    async def fetch(*args, **kwargs):
        asked.append(args)
        return b"{}"

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", prefix)
    out = tmp_path / "c.py"

    code, err = _usage_exit(
        capsys, ["--service", "warehouse", "--namespace", "prod", "--out", str(out)]
    )

    assert code == 7, err
    assert "CLIFFRACER_SUBJECT_PREFIX" in err and repr(prefix) in err
    assert (
        "--namespace" not in err.split("error:")[-1] and "--service" not in err.split("error:")[-1]
    )
    assert asked == [] and not out.exists()


@pytest.mark.parametrize("prefix", ["a.b", "my-env", "x y"])
def test_a_class_described_in_process_does_not_read_the_environments_prefix(
    monkeypatch, tmp_path, prefix
):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", prefix)
    out = tmp_path / "in_process.py"

    assert main(["--class", WAREHOUSE, "--service", "warehouse", "--out", str(out)]) == 0

    assert out.exists()


def test_CONTROL_a_good_subject_prefix_in_the_environment_is_used_on_the_describe_subject(
    monkeypatch, tmp_path
):
    seen = []

    async def fetch(url, service, namespace, *args, **kwargs):
        seen.append(describe_subject(service, namespace))
        from cliffracer.introspect import canonical, describe
        from tests.fixtures.typed_client.service import Warehouse

        return canonical(
            describe(Warehouse, service="warehouse", version="3.1.4").to_dict()
        ).encode()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "prod")
    out = tmp_path / "ok.py"

    assert main(["--service", "warehouse", "--out", str(out)]) == 0

    assert seen == ["prod.warehouse.describe"]


@pytest.mark.parametrize("extra", [[], ["--namespace", "prod"]], ids=["alone", "with-a-namespace"])
def test_a_bad_service_name_is_refused_under_its_own_flag_whether_or_not_a_namespace_is_given(
    monkeypatch, capsys, tmp_path, extra
):
    """The result of one bad flag did not depend on an unrelated flag, and no other flag was blamed."""
    asked = []

    async def fetch(*args, **kwargs):
        asked.append(args)
        return b"{}"

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)

    code, err = _usage_exit(
        capsys, ["--service", "bad name", *extra, "--out", str(tmp_path / "c.py")]
    )

    assert code == 7, err
    reason = err.split("error:")[-1]
    assert "--service:" in reason and "whitespace" in reason and "--namespace" not in reason
    assert asked == []


def test_CONTROL_a_bad_namespace_is_still_blamed_on_the_namespace_with_a_good_service(
    monkeypatch, capsys, tmp_path
):
    code, err = _usage_exit(
        capsys,
        ["--service", "warehouse", "--namespace", "a b", "--out", str(tmp_path / "c.py")],
    )

    assert code == 7, err
    assert "--namespace:" in err and "--service" not in err.split("error:")[-1]


def test_CONTROL_a_bad_namespace_is_still_refused_with_that_prefix_in_the_environment(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "a.b")
    out = tmp_path / "c.py"

    code, err = _usage_exit(
        capsys,
        ["--class", WAREHOUSE, "--service", "w", "--namespace", "a.b", "--out", str(out)],
    )

    assert code == 7, err
    assert "--namespace" in err and "single subject token" in err
    assert "CLIFFRACER_SUBJECT_PREFIX" not in err
    assert not out.exists()


def test_namespace_and_timeout_say_what_they_do(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    text = " ".join(capsys.readouterr().out.split())

    assert "--namespace NAMESPACE" in text and "recorded in the client" in text
    assert "--timeout TIMEOUT without --class" in text

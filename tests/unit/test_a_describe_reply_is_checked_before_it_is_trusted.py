"""A describe reply is checked before the generator trusts it.

In live mode the description is whatever answered on `<service>.describe`.
It was cast to a dict and handed to `Description.from_dict`, and every reader
after that indexes keys it assumes are there, so a reply of the wrong shape
escaped `main()` as a KeyError, TypeError or AttributeError: a traceback and
exit 1, which is not a code the command documents. Each shape below is one
that did. They now exit 4 with a message naming where the reply went wrong,
and write nothing.

Method names are the other half. A generated client is a subclass of
`ServiceClient`, so a method named after anything a client already has --
a class member, an attribute its constructor sets, or a private transport
method -- replaces it.
"""

import json
import types

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import ServiceClient
from cliffracer.core.typed_rpc import UntypedHandler, reserved_rpc_method_names
from cliffracer.generate_client.cli import main
from cliffracer.generate_client.emitter import CannotEmit, emit
from cliffracer.introspect import Description

pytestmark = pytest.mark.unit


def _method(**overrides):
    method = {
        "name": "go",
        "signature_hash": "sha256:s",
        "doc": None,
        "params": [],
        "returns": {"kind": "scalar", "name": "str"},
    }
    method.update(overrides)
    return method


def _reply(**overrides):
    reply = {
        "service": "orders",
        "version": "1",
        "description_hash": "sha256:d",
        "methods": [_method()],
    }
    reply.update(overrides)
    return reply


def _without(mapping, key):
    return {k: v for k, v in mapping.items() if k != key}


MALFORMED = [
    ("no_service", _without(_reply(), "service"), "service is missing"),
    ("no_version", _without(_reply(), "version"), "version is missing"),
    ("an_array", [], "the reply is an array, not an object"),
    ("a_string", "hello", "the reply is a string, not an object"),
    ("null", None, "the reply is null, not an object"),
    ("methods_not_a_list", _reply(methods={"go": 1}), "methods is an object, not an array"),
    (
        "method_without_returns",
        _reply(methods=[_without(_method(), "returns")]),
        "methods[0].returns is missing",
    ),
    (
        "method_name_not_a_string",
        _reply(methods=[_method(name=5)]),
        "methods[0].name is a number, not a string",
    ),
    (
        "param_without_type",
        _reply(methods=[_method(params=[{"name": "x"}])]),
        "methods[0].params[0].type is missing",
    ),
    (
        "type_ref_without_kind",
        _reply(methods=[_method(returns={"name": "str"})]),
        "methods[0].returns.kind is missing",
    ),
    (
        "model_ref_without_module",
        _reply(methods=[_method(returns={"kind": "model", "qualname": "Order"})]),
        "methods[0].returns.module is missing",
    ),
]


def _answer_with(monkeypatch, body):
    async def fetch(*args, **kwargs):
        return json.dumps(body).encode()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)


@pytest.mark.parametrize(
    ("body", "problem"), [c[1:] for c in MALFORMED], ids=[c[0] for c in MALFORMED]
)
def test_a_reply_of_the_wrong_shape_exits_4_naming_where(
    monkeypatch, capsys, tmp_path, body, problem
):
    _answer_with(monkeypatch, body)
    out = tmp_path / "c.py"

    rc = main(["--service", "orders", "--out", str(out)])

    err = capsys.readouterr().err
    assert rc == 4, err
    assert f"orders answered describe with a reply that is not a description: {problem}" in err
    assert not out.exists()


def test_an_unknown_scalar_in_a_reply_exits_4(monkeypatch, capsys, tmp_path):
    """The twelfth shape. Structurally whole, so it reaches the emitter, which
    owns the vocabulary of type names and now refuses it the way it refuses an
    unknown kind."""
    _answer_with(
        monkeypatch, _reply(methods=[_method(returns={"kind": "scalar", "name": "bytes"})])
    )
    out = tmp_path / "c.py"

    rc = main(["--service", "orders", "--out", str(out)])

    assert rc == 4
    assert "unknown scalar type 'bytes'" in capsys.readouterr().err
    assert not out.exists()


def test_emit_refuses_an_unknown_scalar_by_name():
    desc = Description.from_dict(
        _reply(methods=[_method(returns={"kind": "scalar", "name": "bytes"})])
    )

    with pytest.raises(CannotEmit, match="unknown scalar type 'bytes'"):
        emit(desc)


def test_CONTROL_a_well_formed_reply_is_written(monkeypatch, tmp_path):
    _answer_with(monkeypatch, _reply())
    out = tmp_path / "c.py"

    assert main(["--service", "orders", "--out", str(out)]) == 0
    assert "async def go(self) -> str:" in out.read_text()


@pytest.mark.parametrize("name", ["_call", "__init__", "connect_timeout"])
def test_a_reply_naming_a_client_member_is_refused(monkeypatch, capsys, tmp_path, name):
    _answer_with(monkeypatch, _reply(methods=[_method(name=name)]))
    out = tmp_path / "c.py"

    rc = main(["--service", "orders", "--out", str(out)])

    err = capsys.readouterr().err
    assert rc == 4, err
    assert name in err
    assert not out.exists()


def test_a_service_cannot_declare_a_handler_named_after_a_client_attribute():
    class Timing(CliffracerService):
        @rpc
        async def connect_timeout(self, x: int) -> int:
            return x

    service = Timing(ServiceConfig(name="timing"))

    with pytest.raises(
        UntypedHandler, match="connect_timeout.*conflicts with ServiceClient member"
    ):
        service.container.discover_handlers()


def test_every_public_name_a_client_has_is_reserved():
    """Class members and the attributes the constructor sets, read off a real
    client, so a new attribute is reserved without anyone editing a list."""
    client = ServiceClient(service="orders", verify=False)
    public = {n for n in set(dir(ServiceClient)) | set(vars(client)) if not n.startswith("_")}

    assert "connect_timeout" in public, sorted(public)
    assert public <= reserved_rpc_method_names(), sorted(public - reserved_rpc_method_names())


# --- a reply that is not JSON ------------------------------------------------
#
# The decode used to run inside the call that talks to the broker, so a reply
# that was not JSON raised inside the same `try` as a refused connection and
# was reported as "no broker reachable" -- after the broker had answered. These
# go through the real `fetch_description` with only the connection replaced.


class _Connection:
    def __init__(self, *, data: bytes = b"", raises: BaseException | None = None):
        self.data, self.raises = data, raises

    async def request(self, *args, **kwargs):
        if self.raises is not None:
            raise self.raises
        return types.SimpleNamespace(data=self.data, headers={})

    async def close(self):
        return None


def _connect_to(monkeypatch, **kwargs):
    async def connect(*args, **_):
        return _Connection(**kwargs)

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)


NOT_JSON = [
    ("an_html_page", b"<html>proxy error</html>", "'<html>proxy error</html>'"),
    ("an_empty_body", b"", "not JSON: ''"),
    ("invalid_utf8", b"\xff\xfejunk", "junk'"),
]


@pytest.mark.parametrize(("data", "shown"), [c[1:] for c in NOT_JSON], ids=[c[0] for c in NOT_JSON])
def test_a_reply_that_is_not_json_exits_4_showing_it(monkeypatch, capsys, tmp_path, data, shown):
    _connect_to(monkeypatch, data=data)
    out = tmp_path / "c.py"

    rc = main(["--service", "orders", "--out", str(out)])

    err = capsys.readouterr().err
    assert rc == 4, err
    assert "orders answered describe with a reply that is not JSON:" in err
    assert shown in err
    assert "no broker reachable" not in err
    assert not out.exists()


def test_an_error_that_is_not_from_nats_is_not_reported_as_a_missing_broker(monkeypatch, tmp_path):
    """Only a nats error means the broker could not be used. Anything else is a
    defect, and a traceback is the honest report of one."""
    _connect_to(monkeypatch, raises=KeyError("a defect"))

    with pytest.raises(KeyError, match="a defect"):
        main(["--service", "orders", "--out", str(tmp_path / "c.py")])


def test_CONTROL_a_nats_error_still_exits_3(monkeypatch, capsys, tmp_path):
    from nats.errors import ConnectionClosedError

    _connect_to(monkeypatch, raises=ConnectionClosedError())

    assert main(["--service", "orders", "--out", str(tmp_path / "c.py")]) == 3
    assert "no broker reachable" in capsys.readouterr().err


def test_CONTROL_a_json_description_over_the_connection_is_written(monkeypatch, tmp_path):
    _connect_to(monkeypatch, data=json.dumps(_reply()).encode())
    out = tmp_path / "c.py"

    assert main(["--service", "orders", "--out", str(out)]) == 0
    assert "async def go(self) -> str:" in out.read_text()

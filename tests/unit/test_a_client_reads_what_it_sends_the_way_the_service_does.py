"""The client reads an argument the way the service reads a payload: python mode, then JSON mode.

`ServiceClient._encode` checks the argument against its annotation and chooses the wire form the
service accepts and reads back as that argument (`choose_wire_form`). It did both in python mode,
and a strict model refuses there the JSON forms the service now takes, so for a strict model readable
only by its alias both spellings were refused, the by-name dump was sent as the fallback, and the
service refused it though it would have accepted the alias form. The client's reader is now the
service's: one helper, `read_python_then_json`.
"""

import datetime
import uuid

import pytest
from pydantic import BaseModel, ConfigDict, Field

from cliffracer import client as client_module
from cliffracer.client import RpcValidationError, ServiceClient
from cliffracer.core import validation as validation_module

pytestmark = pytest.mark.unit

WHEN = datetime.datetime(2026, 1, 2, 3, 4, 5)
ISO = "2026-01-02T03:04:05"


class StrictAlias(BaseModel):
    """Strict, readable only by its alias, with a type whose JSON form strict python mode refuses."""

    model_config = ConfigDict(strict=True)

    when: datetime.datetime = Field(alias="When")


class StrictPlain(BaseModel):
    model_config = ConfigDict(strict=True)

    when: datetime.datetime
    ident: uuid.UUID


class StrictNameOnly(BaseModel):
    """Strict, dumped by its alias but readable only by its name, so the first form is refused."""

    model_config = ConfigDict(
        strict=True, serialize_by_alias=True, validate_by_name=True, validate_by_alias=False
    )

    when: datetime.datetime = Field(alias="When")


class StrictInner(BaseModel):
    model_config = ConfigDict(strict=True)

    inner_when: datetime.datetime = Field(alias="innerWhen")


class StrictOuter(BaseModel):
    model_config = ConfigDict(strict=True)

    outer: StrictInner = Field(alias="Outer")


@pytest.fixture
def client():
    return ServiceClient(service="s", nats_url="nats://broker.invalid:6999", verify=False)


def test_a_strict_model_readable_only_by_its_alias_is_sent_by_its_alias(client):
    assert client._encode(StrictAlias(When=WHEN), StrictAlias) == {"When": ISO}


def test_a_strict_alias_only_model_in_a_list_and_nested_is_sent_by_its_alias(client):
    assert client._encode([StrictAlias(When=WHEN)], list[StrictAlias]) == [{"When": ISO}]
    nested = StrictOuter(Outer=StrictInner(innerWhen=WHEN))

    assert client._encode(nested, StrictOuter) == {"Outer": {"innerWhen": ISO}}


def test_CONTROL_a_strict_model_readable_only_by_its_name_is_sent_by_its_name(client):
    assert client._encode(StrictNameOnly(when=WHEN), StrictNameOnly) == {"when": ISO}


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")  # the dump of a dict argument
def test_a_dict_in_the_json_form_of_a_strict_model_is_not_refused_before_sending(client):
    """The service takes it, so the client's own check does not turn it away."""
    dump = {"when": ISO, "ident": str(uuid.UUID(int=3))}

    assert client._encode(dump, StrictPlain) == dump


def test_a_dict_a_strict_model_should_refuse_is_still_refused_before_sending(client):
    with pytest.raises(RpcValidationError, match="refused before sending"):
        client._encode({"when": ISO, "ident": "not-a-uuid"}, StrictPlain)
    with pytest.raises(RpcValidationError, match="refused before sending"):
        client._encode({"when": 1700000000, "ident": str(uuid.UUID(int=3))}, StrictPlain)


def test_the_client_and_the_service_read_through_the_one_helper(monkeypatch, client):
    """A copy put back inline would behave the same until the next rule moves."""
    assert client_module.read_python_then_json is validation_module.read_python_then_json
    reads = {"client": 0, "service": 0}

    def spy(name):
        real = validation_module.read_python_then_json

        def counted(payload, in_python, in_json):
            reads[name] += 1
            return real(payload, in_python, in_json)

        return counted

    monkeypatch.setattr(client_module, "read_python_then_json", spy("client"))
    client._encode(StrictAlias(When=WHEN), StrictAlias)
    monkeypatch.setattr(validation_module, "read_python_then_json", spy("service"))
    validation_module.validate_decoded(StrictPlain, {"when": ISO, "ident": str(uuid.UUID(int=3))})

    assert reads["client"] >= 2 and reads["service"] == 1, reads

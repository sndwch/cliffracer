"""Five small things a `ServiceClient` got wrong, each a report or a trace that was lost in silence.

1. A caller's correlation id under another header case (`X-Correlation-Id`, the usual HTTP spelling)
   was ignored and replaced by a new one, which the service, matching case-insensitively, then took.
2. `service`, `namespace` and `subject_prefix` were not checked, so a value `ServiceConfig` would refuse
   built a subject nothing serves and the call waited out `timeout`; with white space it was a
   malformed frame on a connection that may be the application's own.
3. A `verify()` that raised `ClientOutOfDate` left `_verified` as an earlier success had set it, so the
   next call skipped the check and the drift went unreported.
4. A describe that failed during re-verification replaced the service's validation reply with its own
   timeout or connection error: the caller handled "the service is down" instead of "your argument
   is wrong".
5. A dial nats-py retried after an authorization failure was reported as "did not answer within
   connect_timeout", and the reason appeared only in nats-py's own log.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats import errors

from cliffracer.client import ServiceClient
from cliffracer.core import dial
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.exceptions import (
    ClientOutOfDateError,
    RpcClientError,
    RpcConnectionError,
    RpcTimeoutError,
    RpcValidationError,
)

pytestmark = pytest.mark.unit


# --- 1. the caller's correlation id under another case ------------------------------------------

CASES = [
    "X-Correlation-Id",
    "x-correlation-id",
    "X-CORRELATION-ID",
    "X-Correlation-ID",
    "correlation_id",
    "Correlation_ID",
    "X-Request-ID",
    "x-trace-id",
    "Trace-Id",
]


@pytest.mark.parametrize("name", CASES)
def test_a_callers_correlation_id_wins_under_any_case_of_any_name_the_service_reads(name):
    client = ServiceClient(service="svc", headers={name: "trace-abc"})

    sent = client._headers_for_send()

    assert sent["X-Correlation-ID"] == "trace-abc" and sent["correlation_id"] == "trace-abc"


@pytest.mark.parametrize("name", ["X-Correlation-Id", "x-correlation-id", "CORRELATION_ID"])
def test_the_callers_other_case_is_not_sent_beside_the_one_that_replaces_it(name):
    client = ServiceClient(service="svc", headers={name: "trace-abc", "X-Tenant": "t"})

    sent = client._headers_for_send()

    ids = [key for key in sent if key.lower() in {"x-correlation-id", "correlation_id"}]
    assert sorted(ids) == ["X-Correlation-ID", "correlation_id"], sent
    assert sent["X-Tenant"] == "t", "the caller's other headers are kept"


def test_CONTROL_with_no_id_the_ambient_one_is_used_and_then_a_new_one():
    client = ServiceClient(service="svc", headers={"X-Tenant": "t"})
    token = CorrelationContext.set("ambient-1")
    try:
        assert client._headers_for_send()["X-Correlation-ID"] == "ambient-1"
    finally:
        CorrelationContext.clear()
        del token

    fresh = client._headers_for_send()["X-Correlation-ID"]
    assert len(fresh) == 32 and uuid.UUID(fresh)


def test_CONTROL_an_id_the_caller_set_wins_over_the_ambient_one():
    client = ServiceClient(service="svc", headers={"X-Correlation-Id": "explicit"})
    CorrelationContext.set("ambient-1")
    try:
        assert client._headers_for_send()["X-Correlation-ID"] == "explicit"
    finally:
        CorrelationContext.clear()


# --- 2. the parts of the subject ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "names"),
    [
        pytest.param({"namespace": "a b"}, "namespace", id="namespace-with-a-space"),
        pytest.param({"namespace": "a.b"}, "namespace", id="namespace-with-a-dot"),
        pytest.param({"namespace": "a*"}, "namespace", id="namespace-with-a-wildcard"),
        pytest.param({"subject_prefix": "x.>"}, "subject_prefix", id="prefix-with-a-wildcard"),
        pytest.param({"subject_prefix": "x y"}, "subject_prefix", id="prefix-with-a-space"),
        pytest.param({"subject_prefix": "x.y"}, "subject_prefix", id="prefix-with-a-dot"),
        pytest.param({"service": "a b"}, "service", id="service-with-a-space"),
        pytest.param({"service": "a..b"}, "service", id="service-with-an-empty-token"),
        pytest.param({"service": "a.>"}, "service", id="service-with-a-wildcard"),
    ],
)
def test_a_part_that_cannot_be_in_a_subject_is_refused_when_the_client_is_built(kwargs, names):
    with pytest.raises(ValueError, match=names):
        ServiceClient(**{"service": "svc", **kwargs})


async def test_a_client_that_names_no_service_refuses_the_call_instead_of_waiting():
    client = ServiceClient(nc=AsyncMock(), verify=False)

    with pytest.raises(RpcClientError, match="names no service"):
        await client._call("m", {}, int)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="the-defaults"),
        pytest.param({"namespace": "east"}, id="a-namespace"),
        pytest.param({"namespace": ""}, id="an-empty-namespace-is-none"),
        pytest.param({"subject_prefix": "env_1"}, id="a-prefix"),
        pytest.param({"subject_prefix": ""}, id="an-empty-prefix-is-none"),
        pytest.param({"service": "orders.eu"}, id="a-dotted-service-name"),
    ],
)
def test_CONTROL_a_part_the_service_would_accept_is_accepted(kwargs):
    client = ServiceClient(**{"service": "svc", **kwargs})

    assert client._subject("rpc.m").endswith("rpc.m")


@pytest.mark.parametrize("prefix", ["", "good"])
def test_CONTROL_a_bad_environment_prefix_does_not_refuse_a_client_that_pins_its_own(
    monkeypatch, prefix
):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "x y")

    client = ServiceClient(service="orders", subject_prefix=prefix)

    assert client._subject("rpc.m").startswith(f"{prefix}." if prefix else "orders.")


def test_CONTROL_a_good_namespace_is_not_refused_for_the_environments_bad_prefix(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "x y")

    client = ServiceClient(service="orders", namespace="east", subject_prefix="good")

    assert client._subject("rpc.m") == "good.east.orders.rpc.m"


def test_a_bad_environment_prefix_is_refused_by_name_when_the_client_takes_it(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "x y")

    with pytest.raises(ValueError, match="CLIFFRACER_SUBJECT_PREFIX") as refused:
        ServiceClient(service="orders")

    assert "'x y'" in str(refused.value) and "dlq_subject" not in str(refused.value)


def test_a_bad_service_is_reported_as_the_service_whatever_the_environment_prefix_is(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "x y")

    with pytest.raises(ValueError) as refused:
        ServiceClient(service="a b", subject_prefix="good")

    message = str(refused.value)
    assert "service='a b'" in message
    assert "CLIFFRACER_SUBJECT_PREFIX" not in message and "dlq_subject" not in message


def test_every_bad_part_is_named_in_one_refusal():
    with pytest.raises(ValueError) as refused:
        ServiceClient(service="a b", namespace="c d", subject_prefix="x y")

    message = str(refused.value)
    assert "service='a b'" in message
    assert "namespace='c d'" in message
    assert "subject_prefix='x y'" in message


# --- 3. and 4. verification ------------------------------------------------------------------------

MATCHING = {
    "service": "svc",
    "version": "1",
    "description_hash": "sha256:d",
    "methods": [{"name": "do", "signature_hash": "sha256:x", "params": [], "returns": None}],
}
DRIFTED = {
    **MATCHING,
    "methods": [{"name": "do", "signature_hash": "sha256:y", "params": [], "returns": None}],
}


class Wire:
    """Answers describe from a settable description and rpc calls from a settable reply."""

    def __init__(self) -> None:
        self.description = MATCHING
        self.describe_fails: BaseException | None = None
        self.rpc_reply = b'{"success":true,"result":1}'
        self.describes = 0

    async def request(self, subject, payload, headers=None):
        if subject.endswith("describe"):
            self.describes += 1
            if self.describe_fails is not None:
                raise self.describe_fails
            return SimpleNamespace(data=json.dumps(self.description).encode(), headers=None)
        return SimpleNamespace(data=self.rpc_reply, headers=None)


def _client(wire: Wire) -> ServiceClient:
    client = ServiceClient(service="svc")
    client.SIGNATURES = {"do": "sha256:x"}
    client._nc = AsyncMock()
    client._request = wire.request  # type: ignore[method-assign]
    return client


async def test_a_verify_that_finds_drift_clears_what_an_earlier_success_set():
    wire = Wire()
    client = _client(wire)
    await client.verify()
    assert client._verified

    wire.description = DRIFTED
    with pytest.raises(ClientOutOfDateError):
        await client.verify()

    assert client._verified is False


async def test_the_next_call_after_a_drifted_verify_verifies_again_and_says_so_again():
    wire = Wire()
    client = _client(wire)
    await client.verify()
    wire.description = DRIFTED
    with pytest.raises(ClientOutOfDateError):
        await client.verify()

    with pytest.raises(ClientOutOfDateError):
        await client._call("do", {}, int)


async def test_CONTROL_a_verify_that_succeeds_marks_the_client_verified_and_calls_skip_it():
    wire = Wire()
    client = _client(wire)

    await client._call("do", {}, int)
    await client._call("do", {}, int)

    assert client._verified and wire.describes == 1


@pytest.mark.parametrize(
    "describe_error",
    [
        pytest.param(RpcTimeoutError("svc.describe did not answer"), id="timeout"),
        pytest.param(RpcConnectionError("the connection was lost"), id="connection-lost"),
    ],
)
async def test_a_describe_that_fails_during_reverification_leaves_the_validation_error(
    describe_error,
):
    wire = Wire()
    client = _client(wire)
    await client.verify()
    wire.rpc_reply = b'{"success":false,"error":"validation failed","code":"validation_failed"}'
    wire.describe_fails = describe_error

    with pytest.raises(RpcValidationError):
        await client._call("do", {}, int)


async def test_CONTROL_a_reverification_that_finds_drift_still_reports_the_drift():
    wire = Wire()
    client = _client(wire)
    await client.verify()
    wire.rpc_reply = b'{"success":false,"error":"validation failed","code":"validation_failed"}'
    wire.description = DRIFTED

    with pytest.raises(ClientOutOfDateError):
        await client._call("do", {}, int)


# --- 5. why a dial failed ---------------------------------------------------------------------------


def _dialling(monkeypatch, *, reports: BaseException | None):
    async def connect(url, *, timeout, **options):
        if reports is not None:
            await options["error_cb"](reports)
        raise TimeoutError

    monkeypatch.setattr(dial, "connect", connect)


async def test_a_dial_that_timed_out_after_the_broker_refused_says_what_it_refused(monkeypatch):
    _dialling(monkeypatch, reports=errors.AuthorizationError())
    client = ServiceClient(service="svc", nats_url="nats://broker.invalid:4222", connect_timeout=1)

    with pytest.raises(RpcConnectionError) as failed:
        await client._connection()

    assert "did not answer within connect_timeout=1s" in str(failed.value)
    assert "AuthorizationError" in str(failed.value)


async def test_the_last_error_the_broker_reported_is_the_one_named(monkeypatch):
    async def connect(url, *, timeout, **options):
        await options["error_cb"](errors.AuthorizationError())
        await options["error_cb"](errors.StaleConnectionError())
        raise TimeoutError

    monkeypatch.setattr(dial, "connect", connect)
    client = ServiceClient(service="svc", nats_url="nats://broker.invalid:4222", connect_timeout=1)

    with pytest.raises(RpcConnectionError, match="StaleConnectionError"):
        await client._connection()


async def test_CONTROL_a_dial_that_timed_out_with_nothing_reported_keeps_its_message(monkeypatch):
    _dialling(monkeypatch, reports=None)
    client = ServiceClient(service="svc", nats_url="nats://broker.invalid:4222", connect_timeout=1)

    with pytest.raises(RpcConnectionError) as failed:
        await client._connection()

    assert (
        str(failed.value) == "nats://broker.invalid:4222 did not answer within connect_timeout=1s"
    )


async def test_the_url_in_the_message_still_has_no_password(monkeypatch):
    _dialling(monkeypatch, reports=errors.AuthorizationError())
    client = ServiceClient(
        service="svc", nats_url="nats://user:SECRET@broker.invalid:4222", connect_timeout=1
    )

    with pytest.raises(RpcConnectionError) as failed:
        await client._connection()

    assert "SECRET" not in str(failed.value)

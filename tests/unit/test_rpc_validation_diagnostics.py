"""RPC ingestion diagnostics obey the selected disclosure policy."""

import pytest
from loguru import logger
from pydantic import ValidationError
from pydantic.version import version_short

from cliffracer import ServiceConfig
from cliffracer.core.validation import deserialize_payload, serialize_payload
from cliffracer.testing import MockMessage
from tests.fixtures.rpc_validation_diagnostics import (
    CANARY,
    OrderService,
    diagnostic_canaries,
    invalid_orders,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def service():
    service = OrderService(ServiceConfig(name="orders", rpc_validation_errors="redacted"))
    service.accepted = []
    service._discover_handlers()
    await service.container._setup_extensions()
    yield service
    await service.container._stop_extensions()


async def dispatch(service, payload, kind="rpc", format="json"):
    data, content_type = serialize_payload(payload, format=format)
    msg = MockMessage(
        "orders.rpc.place",
        data,
        headers={"Content-Type": content_type, "X-Correlation-ID": "orders-validation"},
        reply="_INBOX.orders" if kind == "rpc" else "",
    )
    records = []
    sink = logger.add(lambda message: records.append(str(message)), format="{message}")
    try:
        if kind == "rpc":
            await service.container._handle_rpc_request(msg)
        else:
            await service.container._handle_async_request(msg)
    finally:
        logger.remove(sink)
    response = (
        deserialize_payload(msg.responded_data, content_type=msg.response_headers["Content-Type"])
        if msg.responded_data is not None
        else None
    )
    return response, records


@pytest.mark.parametrize(
    "case,payload", invalid_orders(), ids=lambda value: value if isinstance(value, str) else None
)
@pytest.mark.parametrize("kind", ["rpc", "async_rpc"])
@pytest.mark.parametrize("format", ["json", "msgpack"])
async def test_redacted_schema_failure_covers_all_diagnostic_surfaces(
    service, case, payload, kind, format
):
    response, logs = await dispatch(service, payload, kind, format)
    assert service.accepted == []
    assert len(service.diagnostics.records) == 1
    diagnostic = service.diagnostics.records[0]
    assert diagnostic["details"] == [
        {
            "type": "validation_failed",
            "loc": [],
            "msg": "Invalid RPC payload",
            "input": None,
            "url": f"https://errors.pydantic.dev/{version_short()}/v/validation_failed",
        }
    ]
    assert diagnostic["chain"]
    assert diagnostic["traceback"]
    assert diagnostic_canaries(diagnostic) == set()
    assert diagnostic_canaries(logs) == set()
    if kind == "rpc":
        assert response["code"] == "validation_failed"
        assert response["success"] is False
        assert response["correlation_id"] == "orders-validation"
        assert response["details"] == diagnostic["details"]
    else:
        assert response is None
        assert len(logs) == 1
        assert logs[0].startswith("Async request place failed validation ")


@pytest.mark.parametrize("kind", ["rpc", "async_rpc"])
@pytest.mark.parametrize("expose_internal_errors", [False, True])
async def test_redacted_validator_crash_preserves_internal_classification(
    service, kind, expose_internal_errors
):
    service.config.expose_internal_errors = expose_internal_errors
    response, logs = await dispatch(
        service, {"order": {"units": 1, "authorization": f"crash:{CANARY}"}}, kind
    )
    assert service.accepted == []
    assert diagnostic_canaries(service.diagnostics.records) == set()
    assert diagnostic_canaries(logs) == set()
    assert service.diagnostics.records[0]["details"] is None
    assert logs
    if kind == "rpc":
        assert response["code"] == "internal"
        assert diagnostic_canaries(response) == set()
    else:
        assert response is None


@pytest.mark.parametrize(
    "case,payload", invalid_orders(), ids=lambda value: value if isinstance(value, str) else None
)
async def test_CONTROL_full_diagnostics_retain_payload_evidence(service, case, payload):
    service.config.rpc_validation_errors = "full"
    response, logs = await dispatch(service, payload)
    assert response["code"] == "validation_failed"
    assert diagnostic_canaries(response["details"]) == {CANARY}
    assert diagnostic_canaries(service.diagnostics.records) == {CANARY}
    assert service.accepted == []


@pytest.mark.parametrize("kind", ["rpc", "async_rpc"])
@pytest.mark.parametrize("policy", ["full", "redacted"])
async def test_valid_orders_keep_their_business_effect(service, kind, policy):
    service.config.rpc_validation_errors = policy
    response, logs = await dispatch(service, {"order": {"units": 7, "authorization": CANARY}}, kind)
    assert service.accepted == [7]
    assert service.diagnostics.records == [{"details": None, "chain": [], "traceback": ""}]
    if kind == "rpc":
        assert response["success"] is True
        assert response["result"] == 7
    else:
        assert response is None


@pytest.mark.parametrize("kind", ["rpc", "async_rpc"])
@pytest.mark.parametrize("policy", ["full", "redacted"])
@pytest.mark.parametrize("exception_type", [ValueError, ImportError])
async def test_decoder_failure_obeys_policy_and_keeps_classification(
    service, monkeypatch, kind, policy, exception_type
):
    from cliffracer.core.dispatch import rpc

    service.config.rpc_validation_errors = policy

    def refuse(*args, **kwargs):
        raise exception_type(CANARY)

    monkeypatch.setattr(rpc, "deserialize_payload", refuse)
    response, logs = await dispatch(service, {"order": {"units": 1}}, kind)
    expected = {CANARY} if policy == "full" else set()
    assert diagnostic_canaries(logs) == expected
    assert logs
    assert service.accepted == []
    assert service.diagnostics.records == []
    if kind == "rpc":
        assert response["code"] == (
            "internal" if exception_type is ImportError else "validation_failed"
        )
        assert response["correlation_id"] == "orders-validation"
        assert diagnostic_canaries(response) == expected
    else:
        assert response is None


def test_policy_preserves_default_and_refuses_unknown_values():
    assert ServiceConfig(name="orders").rpc_validation_errors == "full"
    with pytest.raises(ValidationError):
        ServiceConfig(name="orders", rpc_validation_errors="silent")


def test_CONTROL_canary_detector_accepts_public_diagnostics_and_rejects_credentials():
    assert diagnostic_canaries({"loc": [CANARY], "msg": f"invalid {CANARY}"}) == {CANARY}
    assert (
        diagnostic_canaries(
            {
                "msg": "Invalid RPC payload",
                "input": None,
                "url": f"https://errors.pydantic.dev/{version_short()}/v/validation_failed",
            }
        )
        == set()
    )

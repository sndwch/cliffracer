"""A handler can classify semantic invalidity after its request schema has parsed.

An RPC's broad configuration mapping can be structurally valid while a selected implementation
does not recognise one of its keys. The handler's deliberate ``RpcValidationError`` is a caller
error on request/reply and fire-and-forget paths. It keeps structured evidence under the full
policy, replaces it under the redacted policy, and never turns unrelated handler exceptions into
validation failures.
"""

from __future__ import annotations

import json

import pytest
from loguru import logger
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, async_rpc, rpc
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.testing import MockMessage
from tests.fixtures.rpc_validation_diagnostics import DiagnosticCapture, diagnostic_canaries

pytestmark = pytest.mark.unit

CANARY = "credential_canary_handler"
DETAILS = [
    {
        "type": "unknown_parameter",
        "loc": ["parameters", "threshold"],
        "msg": "threshold is not supported",
        "input": CANARY,
    }
]


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("detail rendering failed")


class Quantity(BaseModel):
    value: int


def unwritable_details(shape: str) -> list[dict]:
    if shape == "bad_string":
        return [{"input": Unprintable()}]
    if shape == "deep_structure":
        value: dict = {}
        # This remains JSON-compatible and serializable on its own, but exceeds
        # the wire serializer's depth once nested in the RPC error envelope.
        for _ in range(300):
            value = {"nested": value}
        return [{"input": value}]
    cyclic: dict = {}
    cyclic["self"] = cyclic
    return [cyclic]


class SemanticService(CliffracerService):
    @rpc
    async def evaluate(self, parameters: dict[str, str]) -> int:
        if "threshold" in parameters:
            raise RpcValidationError(DETAILS, message=f"unsupported {CANARY}")
        return len(parameters)

    @async_rpc
    async def evaluate_later(self, parameters: dict[str, str]) -> None:
        if "threshold" in parameters:
            raise RpcValidationError(DETAILS, message=f"unsupported {CANARY}")

    @rpc
    async def crashes(self) -> int:
        raise ValueError("implementation broke")

    @rpc
    async def pydantic_crashes(self) -> int:
        Quantity.model_validate({"value": "broken"})
        return 1

    @rpc
    async def refuses_with_unwritable_details(self, shape: str) -> int:
        raise RpcValidationError(unwritable_details(shape))

    @async_rpc
    async def refuses_with_unwritable_details_later(self, shape: str) -> None:
        raise RpcValidationError(unwritable_details(shape))


class ObservedSemanticService(CliffracerService):
    diagnostics = DiagnosticCapture()

    @rpc
    async def evaluate(self, secret: str) -> int:
        raise RpcValidationError([{"input": secret}], message=secret)

    @async_rpc
    async def evaluate_later(self, secret: str) -> None:
        raise RpcValidationError([{"input": secret}], message=secret)


async def _dispatch(
    method: str,
    *,
    policy: str = "full",
    reply: bool = True,
    payload: dict | None = None,
):
    service = SemanticService(
        ServiceConfig(name="semantic", rpc_validation_errors=policy, health_listener=False)
    )
    service._discover_handlers()
    records: list[str] = []
    sink = logger.add(lambda message: records.append(str(message)), format="{message}")
    try:
        path = "rpc" if reply else "async"
        if payload is None:
            payload = (
                {"parameters": {"threshold": CANARY}}
                if method in {"evaluate", "evaluate_later"}
                else {}
            )
        msg = MockMessage(
            f"semantic.{path}.{method}",
            json.dumps(payload).encode(),
            headers={"X-Correlation-ID": "semantic-refusal"},
            reply="_INBOX.semantic" if reply else "",
        )
        if reply:
            await service.container._handle_rpc_request(msg)
        else:
            await service.container._handle_async_request(msg)
    finally:
        logger.remove(sink)
    response = json.loads(msg.responded_data) if msg.responded_data is not None else None
    return response, records


async def test_a_handlers_semantic_refusal_is_a_validation_reply_with_its_details():
    response, records = await _dispatch("evaluate")

    assert response["success"] is False
    assert response["code"] == "validation_failed"
    assert response["error"] == "validation failed"
    assert response["details"] == DETAILS
    assert "traceback" not in response
    assert any("failed validation" in line for line in records)


async def test_a_redacted_semantic_refusal_replaces_every_handler_diagnostic():
    response, records = await _dispatch("evaluate", policy="redacted")

    assert response["code"] == "validation_failed"
    assert response["details"][0]["type"] == "validation_failed"
    assert CANARY not in json.dumps(response)
    assert CANARY not in "".join(records)


@pytest.mark.parametrize("policy", ["full", "redacted"])
@pytest.mark.parametrize("reply", [True, False])
async def test_semantic_refusal_obeys_policy_before_result_hooks(policy, reply):
    service = ObservedSemanticService(
        ServiceConfig(name="observed_semantic", rpc_validation_errors=policy, health_listener=False)
    )
    service._discover_handlers()
    await service.container._setup_extensions()
    try:
        path = "rpc" if reply else "async"
        method = "evaluate" if reply else "evaluate_later"
        msg = MockMessage(
            f"observed_semantic.{path}.{method}",
            json.dumps({"secret": CANARY}).encode(),
            reply="_INBOX.semantic" if reply else "",
        )
        if reply:
            await service.container._handle_rpc_request(msg)
            assert json.loads(msg.responded_data)["code"] == "validation_failed"
        else:
            await service.container._handle_async_request(msg)

        assert len(service.diagnostics.records) == 1
        expected = {CANARY} if policy == "full" else set()
        assert diagnostic_canaries(service.diagnostics.records) == expected
    finally:
        await service.container._stop_extensions()


@pytest.mark.parametrize("policy", ["full", "redacted"])
async def test_a_fire_and_forget_semantic_refusal_is_logged_as_validation(policy):
    response, records = await _dispatch("evaluate_later", policy=policy, reply=False)

    assert response is None
    assert any("Async request evaluate_later failed validation" in line for line in records)
    assert not any("Error handling async request" in line for line in records)
    if policy == "redacted":
        assert CANARY not in "".join(records)
    else:
        assert CANARY in "".join(records)


@pytest.mark.parametrize("method", ["crashes", "pydantic_crashes"])
async def test_an_unrelated_handler_exception_remains_an_internal_failure(method):
    response, _ = await _dispatch(method)

    assert response["code"] == "internal"
    assert "details" not in response


@pytest.mark.parametrize("shape", ["cycle", "bad_string", "deep_structure"])
@pytest.mark.parametrize("reply", [True, False])
async def test_details_that_cannot_be_written_do_not_cost_the_validation_classification(
    shape, reply
):
    method = "refuses_with_unwritable_details" if reply else "refuses_with_unwritable_details_later"
    response, records = await _dispatch(method, reply=reply, payload={"shape": shape})

    if reply:
        assert response["code"] == "validation_failed"
        assert response["details"] == []
    else:
        assert response is None
        assert any("failed validation" in line for line in records)

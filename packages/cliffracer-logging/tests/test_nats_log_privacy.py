"""Sensitive handler data stays outside the shared log stream."""

import asyncio
import json
from typing import Any

import pytest
from cliffracer_logging import ContextualLogger, LoggingConfig, log_event_handling, log_rpc_calls
from loguru import logger

from cliffracer import ServiceConfig

pytestmark = pytest.mark.unit


class RecordingNats:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.payloads.append(payload)


async def _published_record(**context: Any) -> tuple[dict[str, Any], bytes]:
    nc = RecordingNats()
    sink_id = LoggingConfig.add_nats_sink(
        service_name="checkout",
        nats_connection=nc,
        config=ServiceConfig(name="checkout", health_port=0),
    )
    try:
        logger.bind(**{"service": "checkout", **context}).info("payment authorized")
        for _ in range(100):
            await asyncio.sleep(0.01)
            for payload in nc.payloads:
                decoded = json.loads(payload)
                if decoded["record"]["message"] == "payment authorized":
                    return decoded, payload
    finally:
        logger.remove(sink_id)
    raise AssertionError("the payment log record was not published")


async def test_nats_log_records_redact_nested_credentials_and_keep_operational_context():
    record, wire = await _published_record(
        customer_id="customer-42",
        password="correct horse battery staple",
        payment={
            "access_token": "payment-token-canary",
            "card": {"authorization": "card-authorization-canary"},
        },
    )

    extra = record["record"]["extra"]
    assert extra["customer_id"] == "customer-42"
    assert extra["password"] == "[REDACTED]"
    assert extra["payment"]["access_token"] == "[REDACTED]"
    assert extra["payment"]["card"]["authorization"] == "[REDACTED]"
    assert b"payment-token-canary" not in wire
    assert b"card-authorization-canary" not in wire
    assert b"correct horse battery staple" not in wire


class RecordingContextLogger(ContextualLogger):
    def __init__(self) -> None:
        self.contexts: list[dict[str, Any]] = []

    def with_context(self, **kwargs: Any) -> "RecordingContextLogger":
        self.contexts.append(kwargs)
        return self

    def info(self, message: str, **kwargs: Any) -> None:
        pass

    def debug(self, message: str, **kwargs: Any) -> None:
        pass

    def error(self, message: str, **kwargs: Any) -> None:
        pass


class CheckoutService:
    config = ServiceConfig(name="checkout", health_port=0)


async def test_rpc_logging_records_argument_shape_without_argument_values():
    recording = RecordingContextLogger()

    @log_rpc_calls(recording)
    async def authorize(service: CheckoutService, account: str, *, password: str) -> None:
        return None

    await authorize(CheckoutService(), "customer-42", password="rpc-password-canary")

    assert recording.contexts == [
        {
            "rpc_method": "authorize",
            "service": "checkout",
            "request_arg_names": ("password",),
            "positional_arg_count": 1,
        }
    ]
    assert "customer-42" not in repr(recording.contexts)
    assert "rpc-password-canary" not in repr(recording.contexts)


async def test_event_logging_records_argument_shape_without_argument_values():
    recording = RecordingContextLogger()

    @log_event_handling(recording)
    async def payment_received(
        service: CheckoutService, subject: str, *, access_token: str
    ) -> None:
        return None

    await payment_received(
        CheckoutService(), "payments.received", access_token="event-token-canary"
    )

    assert recording.contexts == [
        {
            "event_handler": "payment_received",
            "service": "checkout",
            "event_arg_names": ("access_token",),
            "positional_arg_count": 1,
        }
    ]
    assert "payments.received" not in repr(recording.contexts)
    assert "event-token-canary" not in repr(recording.contexts)

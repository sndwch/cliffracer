"""A service changes only the stream fields it declares."""

from dataclasses import fields
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

from cliffracer.core.jetstream import StreamSpec, ensure_streams

pytestmark = pytest.mark.unit

DECLARED_FIELDS = {
    "name",
    "subjects",
    "storage",
    "retention",
    "max_age",
    "duplicate_window",
}


class Page:
    def __init__(self, config: StreamConfig) -> None:
        self.total = 1
        self._info = SimpleNamespace(config=config)

    def __iter__(self):
        return iter([self._info])


class RecordingJetStream:
    def __init__(self, config: StreamConfig) -> None:
        self.page = Page(config)
        self.updated: StreamConfig | None = None

    async def streams_info_iterator(self, offset: int = 0) -> Page:
        return self.page

    async def update_stream(self, config: StreamConfig) -> None:
        self.updated = config

    async def add_stream(self, config: Any) -> None:  # pragma: no cover
        raise AssertionError("the existing audit stream must be updated")


def provisioned_audit_stream() -> StreamConfig:
    return StreamConfig(
        name="AUDIT",
        description="regulated order history",
        subjects=["audit.orders.created"],
        retention=RetentionPolicy.LIMITS,
        max_consumers=20,
        max_msgs=1_000,
        max_bytes=10 * 1024 * 1024,
        discard=DiscardPolicy.NEW,
        max_age=3600.0,
        max_msgs_per_subject=50,
        max_msg_size=32_000,
        storage=StorageType.FILE,
        num_replicas=3,
        duplicate_window=60.0,
        deny_delete=True,
        deny_purge=True,
        metadata={"owner": "platform"},
    )


async def test_adding_an_audit_subject_preserves_every_operator_owned_field():
    current = provisioned_audit_stream()
    js = RecordingJetStream(current)
    logger = MagicMock()
    declaration = StreamSpec(
        name="AUDIT",
        subjects=["audit.orders.created", "audit.orders.refunded"],
        max_age_seconds=7200.0,
        duplicate_window_seconds=120.0,
    )

    await ensure_streams(js, [declaration], allow_update=True, logger=logger)

    assert js.updated is not None
    for field in fields(StreamConfig):
        if field.name not in DECLARED_FIELDS:
            assert getattr(js.updated, field.name) == getattr(current, field.name), field.name
    assert js.updated.subjects == ["audit.orders.created", "audit.orders.refunded"]
    assert js.updated.max_age == 7200.0
    assert js.updated.duplicate_window == 120.0
    logger.info.assert_called_once()
    message = logger.info.call_args.args[0]
    assert "subjects:" in message
    assert "max_age_seconds:" in message
    assert "duplicate_window_seconds:" in message


async def test_declared_field_matching_does_not_claim_to_compare_operator_limits():
    declaration = StreamSpec(
        name="AUDIT",
        subjects=["audit.orders.created"],
        max_age_seconds=3600.0,
        duplicate_window_seconds=60.0,
    )

    assert declaration.matches_declared_fields(provisioned_audit_stream())


async def test_a_matching_declaration_does_not_rewrite_operator_configuration():
    current = provisioned_audit_stream()
    js = RecordingJetStream(current)
    js.update_stream = AsyncMock()
    declaration = StreamSpec(
        name="AUDIT",
        subjects=["audit.orders.created"],
        max_age_seconds=3600.0,
        duplicate_window_seconds=60.0,
    )

    await ensure_streams(js, [declaration], allow_update=True)

    js.update_stream.assert_not_awaited()

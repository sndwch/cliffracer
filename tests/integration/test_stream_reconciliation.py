"""Stream reconciliation preserves broker configuration over the wire."""

import nats
import pytest
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

from cliffracer.core.jetstream import StreamSpec, ensure_streams
from tests.broker_isolation import prefixed_name, prefixed_subject
from tests.conftest import broker_url

pytestmark = pytest.mark.integration


@pytest.mark.nats_required
async def test_subject_reconciliation_preserves_audit_retention_limits():
    name = prefixed_name("RECONCILE_AUDIT")
    created = prefixed_subject("audit.orders.created")
    refunded = prefixed_subject("audit.orders.refunded")
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    try:
        try:
            await js.delete_stream(name)
        except Exception:
            pass
        await js.add_stream(
            config=StreamConfig(
                name=name,
                description="regulated order history",
                subjects=[created],
                retention=RetentionPolicy.LIMITS,
                max_msgs=1_000,
                max_bytes=10 * 1024 * 1024,
                discard=DiscardPolicy.NEW,
                max_age=3600.0,
                max_msgs_per_subject=50,
                storage=StorageType.FILE,
                duplicate_window=60.0,
            )
        )

        await ensure_streams(
            js,
            [
                StreamSpec(
                    name=name,
                    subjects=[created, refunded],
                    max_age_seconds=3600.0,
                    duplicate_window_seconds=60.0,
                )
            ],
            allow_update=True,
        )

        updated = (await js.stream_info(name)).config
        assert updated.subjects == [created, refunded]
        assert updated.description == "regulated order history"
        assert updated.max_msgs == 1_000
        assert updated.max_bytes == 10 * 1024 * 1024
        assert updated.max_msgs_per_subject == 50
        assert updated.discard == DiscardPolicy.NEW
    finally:
        try:
            await js.delete_stream(name)
        except Exception:
            pass
        await nc.close()

"""The stream subjects `StreamSpec` refuses are the ones the broker refuses."""

import nats
import pytest
from nats.js.api import StreamConfig
from nats.js.errors import ServerError
from pydantic import ValidationError

from cliffracer.core.jetstream import StreamSpec, ensure_streams
from tests.broker_isolation import prefixed_name, prefixed_subject
from tests.conftest import broker_url

pytestmark = pytest.mark.integration

# Subjects whose first token is a wildcard, with the rest of the shape varying.
LEADING_WILDCARD = ["*.events.shapes.*", "*.shapes", "*.*", "*.>", ">"]
# Subjects a stream may claim, each under this run's prefix so that a shared
# broker's other streams are not overlapped.
LITERAL_FIRST_TOKEN = ["shapes.events.*", "shapes.*.>", "shapes.>"]


@pytest.mark.nats_required
@pytest.mark.parametrize("subject", LEADING_WILDCARD)
async def test_the_broker_refuses_what_the_declaration_refuses(subject):
    with pytest.raises(ValidationError):
        StreamSpec(name="SHAPES", subjects=[subject])

    nc = await nats.connect(broker_url())
    try:
        with pytest.raises(ServerError) as exc:
            await nc.jetstream().add_stream(
                config=StreamConfig(name=prefixed_name("SHAPES_REFUSED"), subjects=[subject])
            )
        assert exc.value.err_code == 10052
    finally:
        await nc.close()


@pytest.mark.nats_required
@pytest.mark.parametrize("subject", LITERAL_FIRST_TOKEN)
async def test_the_broker_accepts_what_the_declaration_accepts(subject):
    claimed = prefixed_subject(subject)
    name = prefixed_name("SHAPES_ACCEPTED")
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    try:
        try:
            await js.delete_stream(name)
        except Exception:
            pass
        spec = StreamSpec(name=name, subjects=[claimed], storage="memory")
        await ensure_streams(js, [spec])

        assert (await js.stream_info(name)).config.subjects == [claimed]
    finally:
        try:
            await js.delete_stream(name)
        except Exception:
            pass
        await nc.close()

"""A marker read by its reason, and a bucket made with or without a placement.

A message whose marker reason is none of the reasons KV knows is returned as it was read. A bucket
made with a placement is made through a scoped copy of the context and then answers to the shared
context. Without a placement, or with a context that is not a `JetStreamContext`, the context given
makes the bucket itself.
"""

import asyncio

import pytest
from cliffracer_kv.markers import _MarkerAwareJetStream
from cliffracer_kv.provisioning import create_bucket
from nats.js.api import RawStreamMsg
from nats.js.client import JetStreamContext
from nats.js.kv import KV_MARKER_REASON, KV_OP

pytestmark = pytest.mark.unit


class Reads:
    def __init__(self, message):
        self.message = message

    async def get_msg(self, *args, **kwargs):
        return self.message


def test_a_message_whose_marker_reason_is_unknown_is_returned_unchanged():
    message = RawStreamMsg(subject="$KV.b.k", seq=1, data=b"", headers={KV_MARKER_REASON: "Other"})

    read = asyncio.run(_MarkerAwareJetStream(Reads(message)).get_msg("s", seq=1))

    assert read is message and KV_OP not in read.headers


class Bucket:
    pass


class Context(JetStreamContext):
    """A JetStream context with no connection: it records what it was asked to do."""

    def __init__(self):
        self.calls = []

    async def create_key_value(self, **params):
        self.calls.append(("create_key_value", self, params))
        bucket = Bucket()
        bucket._js = self
        await self.add_stream(None, name="KV_b")
        return bucket

    async def add_stream(self, config=None, **options):
        self.calls.append(("add_stream", self, options))


def test_a_bucket_made_with_a_placement_answers_to_the_shared_context():
    context = Context()

    bucket = asyncio.run(create_bucket(context, bucket="b", placement="P"))

    assert bucket._js is context
    assert ("add_stream", context, {"name": "KV_b", "placement": "P"}) in context.calls


def test_without_a_placement_the_given_context_makes_the_bucket_itself():
    context = Context()

    asyncio.run(create_bucket(context, bucket="b"))

    assert [call[1] for call in context.calls] == [context, context]
    assert context.calls[-1] == ("add_stream", context, {"name": "KV_b"})


def test_a_context_that_is_not_a_jetstream_context_makes_the_bucket_as_it_is():
    class Plain:
        def __init__(self):
            self.made = []

        async def create_key_value(self, **params):
            self.made.append(params)
            return Bucket()

    context = Plain()

    bucket = asyncio.run(create_bucket(context, bucket="b", placement="P"))

    assert context.made == [{"bucket": "b", "placement": "P"}] and not hasattr(bucket, "_js")

"""`@idempotent` on a generator is refused, and `Nats-Msg-Id`'s bound is measured in bytes.

Calling a generator function only builds the generator, so the wrapper reset the key before
the body ran: the decorator looked applied (the marker was set, the key was extracted) and no
message ever carried an id. It is refused at decoration, naming the function.

`format_nats_msg_id` documents a bound on the header, which goes on the wire as UTF-8, and
measured characters: a key of non-ASCII text could be well over it. Both thresholds are bytes.
"""

import hashlib

import pytest

from cliffracer import idempotent
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.idempotency import format_nats_msg_id

pytestmark = pytest.mark.unit


def test_an_async_generator_is_refused_at_decoration_and_named():
    with pytest.raises(ConfigurationError, match="stream_orders"):

        @idempotent(key="order_id")
        async def stream_orders(order_id: str):
            yield order_id


def test_a_plain_generator_is_refused_too():
    with pytest.raises(ConfigurationError, match="generator"):

        @idempotent(key="order_id")
        def stream_orders(order_id: str):
            yield order_id


def test_CONTROL_a_coroutine_and_a_function_are_still_decorated():
    @idempotent(key="order_id")
    async def place(order_id: str):
        return order_id

    @idempotent(key="order_id")
    def place_sync(order_id: str):
        return order_id

    assert place._cliffracer_idempotent is True
    assert place_sync._cliffracer_idempotent is True


def test_a_non_ascii_key_over_the_byte_bound_is_hashed():
    key = "é" * 100  # 100 characters, 200 bytes

    value = format_nats_msg_id("orders", key)

    assert value == "orders:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def test_the_whole_value_is_measured_in_bytes_too():
    subject = "café." * 12  # 60 characters, 72 bytes
    key = "k" * 60  # the subject and key together are 121 characters, 133 bytes

    value = format_nats_msg_id(subject, key)

    assert len(value.encode("utf-8")) <= 128


def test_CONTROL_an_ascii_key_keeps_the_id_it_had_up_to_the_bound():
    assert format_nats_msg_id("o", "k" * 100) == "o:" + "k" * 100
    assert format_nats_msg_id("o", "k" * 200) == "o:" + hashlib.sha256(b"k" * 200).hexdigest()

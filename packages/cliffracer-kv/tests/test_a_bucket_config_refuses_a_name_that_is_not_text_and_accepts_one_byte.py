"""What a bucket configuration accepts at its bounds and how it names what it refuses.

A dictionary declaration whose name is not text is refused for its name. A size of one byte is a
size. A `RePublish` given inside a dictionary declaration is kept as it is.
"""

import pytest
from cliffracer_kv import BucketConfigError
from cliffracer_kv.config import BucketConfig
from nats.js.api import RePublish

pytestmark = pytest.mark.unit


def test_a_dictionary_whose_name_is_not_text_is_refused_for_its_name():
    with pytest.raises(BucketConfigError, match="must contain a valid string 'name' or 'bucket'"):
        BucketConfig.from_value({"name": 5})


@pytest.mark.parametrize("field", ["max_bytes", "max_value_size"])
def test_a_size_of_one_byte_is_accepted(field):
    assert getattr(BucketConfig(name="b", **{field: 1}), field) == 1


def test_a_republish_given_as_a_republish_in_a_dictionary_is_kept():
    republish = RePublish(src=">", dest="mirror.>")

    assert BucketConfig.from_value({"name": "b", "republish": republish}).republish is republish

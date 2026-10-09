"""A bucket or object-store configuration that cannot work is refused, by name, when it is built."""

import math
from datetime import timedelta

import pytest
from cliffracer_kv import BucketConfig, KvError, ObjectStoreConfig
from cliffracer_kv.config import normalize_ttl_seconds
from cliffracer_kv.errors import BucketConfigError
from nats.js.api import StorageType

pytestmark = pytest.mark.unit

CONFIGS = [BucketConfig, ObjectStoreConfig]


# --- an option nobody reads ----------------------------------------------------


@pytest.mark.parametrize("config", CONFIGS)
def test_a_dictionary_with_an_unknown_option_is_refused_naming_it(config):
    """A misspelled option used to vanish, and the bucket was built without it."""
    with pytest.raises(BucketConfigError) as caught:
        config.from_value({"name": "x", "replicas": 1, "hisotry": 5, "max_age": 30})

    message = str(caught.value)
    assert "'hisotry'" in message and "'max_age'" in message, message
    assert "'x'" in message, message
    assert "replicas" not in message.split("unknown option(s)")[1].split(";")[0], message


@pytest.mark.parametrize("config", CONFIGS)
def test_CONTROL_every_option_a_config_has_is_accepted_in_a_dictionary(config):
    """The refusal is of names the config does not have, not of the dictionary form."""
    from dataclasses import fields

    safe = {"name": "x", "description": "d", "max_bytes": 10, "replicas": 1, "storage": "memory"}
    safe = {k: v for k, v in safe.items() if k in {f.name for f in fields(config)}}

    assert config.from_value(safe).name == "x"
    assert config.from_value({"bucket": "x"}).name == "x"


# --- what None means -----------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: BucketConfig.from_value({"name": "x"}, default_ttl=120), id="bucket-key-absent"
        ),
        pytest.param(
            lambda: BucketConfig.from_value({"name": "x", "ttl": None}, default_ttl=120),
            id="bucket-dict-none",
        ),
        pytest.param(
            lambda: BucketConfig.from_value(BucketConfig(name="x"), default_ttl=120),
            id="bucket-instance-none",
        ),
        pytest.param(
            lambda: ObjectStoreConfig.from_value({"name": "x", "ttl": None}, default_ttl=120),
            id="store-dict-none",
        ),
        pytest.param(
            lambda: ObjectStoreConfig.from_value(ObjectStoreConfig(name="x"), default_ttl=120),
            id="store-instance-none",
        ),
    ],
)
def test_an_unset_ttl_takes_the_default_whatever_shape_the_config_came_in(build):
    assert build().ttl == 120


@pytest.mark.parametrize("config", CONFIGS)
def test_CONTROL_an_explicit_ttl_beats_the_default(config):
    assert config.from_value({"name": "x", "ttl": 5}, default_ttl=120).ttl == 5
    assert config.from_value(config(name="x", ttl=5), default_ttl=120).ttl == 5


# --- a ttl the server would refuse ---------------------------------------------


@pytest.mark.parametrize(
    "ttl",
    [
        True,
        False,
        "60",
        b"60",
        -5,
        -0.5,
        timedelta(seconds=-1),
        math.nan,
        math.inf,
        -math.inf,
        0.05,
        timedelta(milliseconds=50),
    ],
    ids=repr,
)
def test_a_ttl_the_server_would_refuse_is_refused_when_it_is_normalised(ttl):
    with pytest.raises(BucketConfigError, match="Invalid TTL value"):
        normalize_ttl_seconds(ttl)


@pytest.mark.parametrize("config", CONFIGS)
def test_a_bad_ttl_is_refused_when_the_config_is_built_naming_the_bucket(config):
    with pytest.raises(BucketConfigError, match=r"'sessions': Invalid TTL value -5"):
        config(name="sessions", ttl=-5)


@pytest.mark.parametrize(
    ("ttl", "seconds"),
    [
        (None, None),
        (0, 0.0),
        (0.1, 0.1),
        (1, 1.0),
        (60.5, 60.5),
        (timedelta(seconds=30), 30.0),
        (timedelta(milliseconds=100), 0.1),
    ],
    ids=repr,
)
def test_CONTROL_a_ttl_the_server_accepts_still_converts(ttl, seconds):
    assert normalize_ttl_seconds(ttl) == seconds


def test_a_bad_default_ttl_is_refused_where_it_is_applied():
    with pytest.raises(BucketConfigError, match="Invalid TTL value"):
        BucketConfig.from_value("x", default_ttl=-1)
    with pytest.raises(BucketConfigError, match="Invalid TTL value"):
        BucketConfig.from_value(BucketConfig(name="x"), default_ttl=0.01)


# --- history, replicas, storage ------------------------------------------------


@pytest.mark.parametrize("history", [0, -1, 65, 100, True, 1.5, "3"], ids=repr)
def test_a_history_outside_one_to_sixty_four_is_refused_naming_the_field(history):
    with pytest.raises(BucketConfigError, match=r"Bucket 'x': history must be"):
        BucketConfig(name="x", history=history)


@pytest.mark.parametrize("history", [1, 5, 64])
def test_CONTROL_a_history_in_range_is_accepted(history):
    assert BucketConfig(name="x", history=history).history == history


@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("replicas", [0, -1, True, 1.5, "3"], ids=repr)
def test_replicas_below_one_or_not_an_integer_are_refused_naming_the_field(config, replicas):
    with pytest.raises(BucketConfigError, match=r"replicas must be"):
        config(name="x", replicas=replicas)


@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("storage", ["FILE", "File", "disk", "", 1, True], ids=repr)
def test_a_storage_that_is_not_file_or_memory_is_refused_naming_the_field(config, storage):
    with pytest.raises(BucketConfigError, match=r"storage must be one of 'file', 'memory'"):
        config(name="x", storage=storage)


@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("storage", [None, "file", "memory", StorageType.FILE, StorageType.MEMORY])
def test_CONTROL_a_valid_storage_is_accepted(config, storage):
    assert config(name="x", storage=storage).storage == storage


def test_every_refusal_is_a_kv_error():
    """A caller catching `KvError` for configuration problems meets all of them."""
    for build in (
        lambda: BucketConfig(name="x", history=0),
        lambda: BucketConfig(name="x", ttl=math.nan),
        lambda: BucketConfig.from_value({"name": "x", "nope": 1}),
        lambda: ObjectStoreConfig(name="x", storage="disk"),
    ):
        with pytest.raises(KvError):
            build()

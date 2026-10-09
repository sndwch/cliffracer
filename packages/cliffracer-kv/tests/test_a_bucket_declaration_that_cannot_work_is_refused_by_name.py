"""A declaration that cannot work is refused by name where it is made, not read as something else.

`KvExtension(buckets="profiles")` iterated the string into eight one-letter buckets; a single
dictionary iterated into its keys. A declaration that was not a name, a configuration or a
dictionary raised `AttributeError` from `declared_name`, and a typo inside a nested `placement` or
`republish` dictionary raised a bare `TypeError` naming no bucket. A name with a dot, a non-numeric
`max_value_size` and a non-bool `direct` built and failed when the extension started, with an error
that is not a `KvError`. A bucket declared twice with different options kept only the last.
"""

import pytest
from cliffracer_kv import BucketConfig, KvError, KvExtension, ObjectStoreConfig
from cliffracer_kv.errors import BucketConfigError

pytestmark = pytest.mark.unit


def declared(**kwargs):
    extension = KvExtension(**kwargs)
    extension._declare()
    return extension


# --- one declaration is one declaration ----------------------------------------------------


def test_a_single_name_is_one_bucket_not_its_letters():
    extension = declared(buckets="profiles")

    assert list(extension._bucket_configs) == ["profiles"]


def test_a_single_object_store_name_is_one_store_not_its_letters():
    extension = declared(object_stores="media")

    assert list(extension._object_store_configs) == ["media"]


def test_a_single_dictionary_is_one_bucket_not_its_keys():
    extension = declared(buckets={"name": "a", "ttl": 5})

    assert list(extension._bucket_configs) == ["a"]
    assert extension._bucket_configs["a"].ttl == 5


def test_a_single_configuration_object_is_one_bucket():
    extension = declared(buckets=BucketConfig("a", history=3))

    assert extension._bucket_configs["a"].history == 3


def test_CONTROL_a_list_of_names_is_still_a_list_of_buckets():
    extension = declared(buckets=["a", "b"], object_stores=["x"])

    assert list(extension._bucket_configs) == ["a", "b"]
    assert list(extension._object_store_configs) == ["x"]


# --- a declaration that is not one is a BucketConfigError ----------------------------------


@pytest.mark.parametrize("bad", [123, None, ["a"], 1.5], ids=["int", "none", "list", "float"])
@pytest.mark.parametrize("argument", ["buckets", "object_stores"])
def test_a_declaration_of_the_wrong_kind_is_refused_as_a_kv_error(argument, bad):
    with pytest.raises(KvError) as caught:
        declared(**{argument: [bad]})

    assert isinstance(caught.value, BucketConfigError)
    assert type(bad).__name__ in str(caught.value), str(caught.value)


@pytest.mark.parametrize(
    ("declaration", "option"),
    [
        ({"name": "a", "placement": {"clusterr": "x"}}, "placement"),
        ({"name": "a", "republish": {"src": ">", "dst": "x"}}, "republish"),
    ],
    ids=["placement", "republish"],
)
def test_a_typo_in_a_nested_option_names_the_bucket_and_the_option(declaration, option):
    with pytest.raises(BucketConfigError) as caught:
        BucketConfig.from_value(declaration)

    message = str(caught.value)
    assert "'a'" in message and option in message, message


# --- a configuration that cannot work is refused when it is built --------------------------


@pytest.mark.parametrize("name", ["user.sessions", "a.b", "", "has space", "wild*", "a>"])
@pytest.mark.parametrize("config", [BucketConfig, ObjectStoreConfig])
def test_a_name_the_broker_refuses_is_refused_when_built(config, name):
    with pytest.raises(BucketConfigError) as caught:
        config(name)

    assert repr(name) in str(caught.value), str(caught.value)


@pytest.mark.parametrize("name", ["orders", "Orders_2", "a-b", "x"])
@pytest.mark.parametrize("config", [BucketConfig, ObjectStoreConfig])
def test_CONTROL_a_name_the_broker_accepts_is_accepted(config, name):
    assert config(name).name == name


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("max_value_size", "x"),
        ("max_value_size", 0),
        ("max_bytes", -5),
        ("max_bytes", "big"),
        ("direct", "yes"),
        ("direct", 1),
        ("description", 5),
        ("placement", "east"),
        ("republish", 3),
        ("limit_marker_ttl", 0.5),
        ("limit_marker_ttl", "soon"),
    ],
)
def test_an_option_that_cannot_work_is_refused_naming_the_bucket_and_the_option(option, value):
    with pytest.raises(BucketConfigError) as caught:
        BucketConfig("a", **{option: value})

    message = str(caught.value)
    assert "'a'" in message and option in message, message


@pytest.mark.parametrize(("option", "value"), [("max_bytes", "big"), ("description", 5)])
def test_an_object_store_option_that_cannot_work_is_refused_by_name(option, value):
    with pytest.raises(BucketConfigError) as caught:
        ObjectStoreConfig("a", **{option: value})

    assert "'a'" in str(caught.value) and option in str(caught.value)


def test_CONTROL_the_documented_values_are_accepted():
    config = BucketConfig(
        "a", max_value_size=1024, max_bytes=-1, direct=True, description="d", limit_marker_ttl=30
    )

    assert config.max_value_size == 1024 and config.direct is True


# --- a bucket declared twice ----------------------------------------------------------------


def test_a_bucket_declared_twice_with_different_options_is_refused_naming_it():
    with pytest.raises(BucketConfigError) as caught:
        declared(buckets=["a", BucketConfig("a", ttl=5), {"name": "a", "history": 3}])

    assert "'a'" in str(caught.value) and "twice" in str(caught.value)


def test_a_bucket_declared_twice_the_same_way_is_one_bucket():
    extension = declared(buckets=["a", "a", {"name": "a"}])

    assert list(extension._bucket_configs) == ["a"]

"""`buckets=` and `object_stores=` that are not a declaration or a collection of them are refused by name.

`KvExtension(buckets=42)` raised `TypeError: 'int' object is not iterable`, naming neither the option
nor the extension, and `buckets=b"abc"` was iterated into the declarations 97, 98 and 99, which
failed later at setup as three declarations of the wrong kind. A number, a boolean, a `bytes` and a
`bytearray` are a `BucketConfigError` naming the option and the value where the extension is built.
A name is still one declaration, and a list, tuple, set or generator of declarations is still read.
"""

import pytest
from cliffracer_kv import BucketConfig, KvError, KvExtension, ObjectStoreConfig
from cliffracer_kv.errors import BucketConfigError

pytestmark = pytest.mark.unit

OPTIONS = ["buckets", "object_stores"]


@pytest.mark.parametrize("option", OPTIONS)
@pytest.mark.parametrize(
    "bad",
    [42, 0, 3.5, True, object(), b"profiles", bytearray(b"profiles"), memoryview(b"ab")],
    ids=["int", "zero", "float", "bool", "object", "bytes", "bytearray", "memoryview"],
)
def test_a_value_that_is_not_a_declaration_or_a_collection_is_refused_naming_the_option(
    option, bad
):
    with pytest.raises(BucketConfigError) as caught:
        KvExtension(**{option: bad})

    message = str(caught.value)
    assert isinstance(caught.value, KvError)
    assert message.startswith(f"{option} takes "), message
    assert type(bad).__name__ in message, message


@pytest.mark.parametrize("option", OPTIONS)
def test_the_refusal_names_the_option_that_was_given_the_bad_value(option):
    other = next(name for name in OPTIONS if name != option)

    with pytest.raises(BucketConfigError) as caught:
        KvExtension(**{option: 42, other: ["fine"]})

    assert str(caught.value).startswith(f"{option} takes ")


@pytest.mark.parametrize("option", OPTIONS)
@pytest.mark.parametrize(
    ("declared", "names"),
    [
        (None, []),
        ("profiles", ["profiles"]),
        (["a", "b"], ["a", "b"]),
        (("a", "b"), ["a", "b"]),
        ({"a"}, ["a"]),
        ({"name": "a"}, ["a"]),
        ([], []),
    ],
    ids=["none", "name", "list", "tuple", "set", "dict", "empty"],
)
def test_CONTROL_a_name_a_collection_a_dictionary_or_nothing_is_still_read(option, declared, names):
    extension = KvExtension(**{option: declared})
    extension._declare()

    configs = extension._bucket_configs if option == "buckets" else extension._object_store_configs
    assert list(configs) == names


@pytest.mark.parametrize(
    ("option", "config"),
    [("buckets", BucketConfig("a")), ("object_stores", ObjectStoreConfig("a"))],
)
def test_CONTROL_a_configuration_object_is_still_one_declaration(option, config):
    extension = KvExtension(**{option: config})
    extension._declare()

    configs = extension._bucket_configs if option == "buckets" else extension._object_store_configs
    assert list(configs) == ["a"]


@pytest.mark.parametrize("option", OPTIONS)
def test_CONTROL_a_generator_of_names_is_still_read(option):
    extension = KvExtension(**{option: (name for name in ["a", "b"])})
    extension._declare()

    configs = extension._bucket_configs if option == "buckets" else extension._object_store_configs
    assert list(configs) == ["a", "b"]

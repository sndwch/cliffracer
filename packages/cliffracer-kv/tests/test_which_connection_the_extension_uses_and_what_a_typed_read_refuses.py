"""Which JetStream context a declaration resolves to, and what `get(as_type=dict|list)` accepts.

What the declaration was given wins over what the service has. A stored value that is not the
requested container type is refused, not returned as whatever it was.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cliffracer_kv import JetStreamUnavailableError, KvExtension
from cliffracer_kv.serialization import deserialize_value

pytestmark = pytest.mark.unit


def _service(*, js=None, nc=None) -> SimpleNamespace:
    return SimpleNamespace(js=js, nc=nc)


def _nc(context: str) -> MagicMock:
    nc = MagicMock()
    nc.jetstream.return_value = context
    return nc


def _extension(service: SimpleNamespace, **declared) -> KvExtension:
    ext = KvExtension(**declared)
    ext.service = service
    return ext


# --- the order a context is chosen in ------------------------------------------


def test_an_explicit_js_wins_over_everything():
    ext = _extension(
        _service(js="service-js", nc=_nc("service-nc-js")), js="explicit-js", nc=_nc("x")
    )

    assert ext._resolve_js() == "explicit-js"


def test_an_explicit_nc_wins_over_the_services_js():
    """The README says `nc=` takes a connection `instead` of the service's own."""
    ext = _extension(_service(js="service-js"), nc=_nc("explicit-nc-js"))

    assert ext._resolve_js() == "explicit-nc-js"


def test_an_explicit_nc_wins_over_the_services_connection():
    ext = _extension(_service(nc=_nc("service-nc-js")), nc=_nc("explicit-nc-js"))

    assert ext._resolve_js() == "explicit-nc-js"


def test_the_services_js_wins_over_a_context_built_from_its_connection():
    ext = _extension(_service(js="service-js", nc=_nc("service-nc-js")))

    assert ext._resolve_js() == "service-js"


def test_a_service_with_no_js_gets_a_context_built_from_its_connection():
    """The default `jetstream_enabled=False` leaves `service.js` None, and KV works regardless."""
    ext = _extension(_service(js=None, nc=_nc("service-nc-js")))

    assert ext._resolve_js() == "service-nc-js"


def test_with_nothing_to_use_the_error_names_what_is_missing_and_not_jetstream_enabled():
    ext = _extension(_service())

    with pytest.raises(JetStreamUnavailableError) as caught:
        ext._resolve_js()

    message = str(caught.value)
    assert "no js= or nc=" in message and "no connection" in message, message
    assert "jetstream_enabled" not in message, message


@pytest.mark.asyncio
async def test_a_declared_bucket_is_opened_through_the_explicit_connection():
    explicit_js = AsyncMock()
    nc = MagicMock()
    nc.jetstream.return_value = explicit_js
    service_js = AsyncMock()
    ext = _extension(_service(js=service_js), nc=nc, buckets=["cache"])

    await ext.start()

    explicit_js.key_value.assert_awaited_once()
    service_js.key_value.assert_not_awaited()


# --- a typed read refuses a value of another shape ----------------------------


@pytest.mark.parametrize(
    ("stored", "as_type", "found"),
    [
        (b"123", dict, "int"),
        (b'"text"', dict, "str"),
        (b"[1, 2]", dict, "list"),
        (b"null", dict, "NoneType"),
        (b"123", list, "int"),
        (b"{}", list, "dict"),
        (b'"text"', list, "str"),
    ],
    ids=repr,
)
def test_a_stored_value_of_another_shape_is_refused(stored, as_type, found):
    with pytest.raises(ValueError) as caught:
        deserialize_value(stored, as_type=as_type)

    assert f"is of type {found}, not {as_type.__name__}" in str(caught.value)


@pytest.mark.parametrize(
    ("stored", "as_type", "expected"),
    [
        (b'{"a": 1}', dict, {"a": 1}),
        (b"{}", dict, {}),
        (b"[1, 2]", list, [1, 2]),
        (b"[]", list, []),
    ],
    ids=repr,
)
def test_CONTROL_a_stored_value_of_the_requested_shape_is_returned(stored, as_type, expected):
    assert deserialize_value(stored, as_type=as_type) == expected


def test_CONTROL_text_that_is_not_json_is_still_a_json_error_for_a_typed_read():
    with pytest.raises(ValueError, match="Expecting value"):
        deserialize_value(b"not json", as_type=dict)


@pytest.mark.asyncio
async def test_get_with_a_container_type_refuses_a_stored_scalar():
    kv = AsyncMock()
    kv.get.return_value = SimpleNamespace(value=b"123", operation=None, revision=1)
    js = AsyncMock()
    js.key_value.return_value = kv
    ext = KvExtension(js=js)

    with pytest.raises(ValueError, match="is of type int, not dict"):
        await ext.get("b", "k", as_type=dict)


# --- a converter is called with the parsed value, then with the text ----------


def test_a_converter_is_called_with_the_parsed_value_and_then_the_text_if_that_raises():
    seen: list[object] = []

    def convert(value):
        seen.append(value)
        raise ValueError(f"cannot convert {value!r}")

    with pytest.raises(ValueError, match="cannot convert '7'"):
        deserialize_value(b"7", as_type=convert)

    assert seen == [7, "7"]


def test_a_converter_that_only_takes_text_is_served_by_the_retry():
    assert deserialize_value(b"123", as_type=lambda value: value.upper()) == "123"

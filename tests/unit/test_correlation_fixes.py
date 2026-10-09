import re

import pytest

from cliffracer.core.correlation import (
    CorrelationContext,
    correlation_id_var,
    create_correlation_id,
    refusal_of,
    with_correlation_id,
)


class DummyRequest:
    def __init__(self, headers):
        self.headers = headers


class MyService:
    @with_correlation_id
    def do_work(self, request, correlation_id=None):
        return correlation_id

    @with_correlation_id
    async def do_work_async(self, request, correlation_id=None):
        return correlation_id


def test_decorated_bound_method_adopts_request_header():
    svc = MyService()
    req = DummyRequest({"x-correlation-id": "test-corr-id-123"})

    # Assert adopted
    cid = svc.do_work(req)
    assert cid == "test-corr-id-123"


@pytest.mark.asyncio
async def test_decorated_bound_method_adopts_request_header_async():
    svc = MyService()
    req = DummyRequest({"x-correlation-id": "test-corr-id-123"})

    # Assert adopted
    cid = await svc.do_work_async(req)
    assert cid == "test-corr-id-123"


def test_create_correlation_id_returns_different_ids():
    token = correlation_id_var.set(None)
    try:
        id1 = create_correlation_id()
        id2 = create_correlation_id()
        assert id1 != id2
        assert correlation_id_var.get() == id2
    finally:
        correlation_id_var.reset(token)


def test_get_or_create_id_no_mutate():
    token = correlation_id_var.set(None)
    try:
        new_id = CorrelationContext.get_or_create_id()
        # Should not have mutated the ambient context
        assert correlation_id_var.get() is None
        assert new_id is not None
    finally:
        correlation_id_var.reset(token)


def test_with_correlation_id_resets_contextvar():
    token = correlation_id_var.set("initial-id")
    try:
        svc = MyService()
        req = DummyRequest({"x-correlation-id": "inner-id"})
        cid = svc.do_work(req)
        assert cid == "inner-id"
        # Context should be reset to initial
        assert correlation_id_var.get() == "initial-id"
    finally:
        correlation_id_var.reset(token)


@pytest.mark.asyncio
async def test_with_correlation_id_resets_contextvar_async():
    token = correlation_id_var.set("initial-id")
    try:
        svc = MyService()
        req = DummyRequest({"x-correlation-id": "inner-id"})
        cid = await svc.do_work_async(req)
        assert cid == "inner-id"
        # Context should be reset to initial
        assert correlation_id_var.get() == "initial-id"
    finally:
        correlation_id_var.reset(token)


def test_invalid_correlation_id_rejected():
    # \r\n should be rejected and replaced
    invalid_id = "test\r\n-id"

    # Test get_or_create_id
    new_id = CorrelationContext.get_or_create_id(invalid_id)
    assert new_id != invalid_id
    assert "\r" not in new_id

    # Test new_id_unless_given
    new_id2 = CorrelationContext.new_id_unless_given(invalid_id)
    assert new_id2 != invalid_id

    # Test extract_from_headers
    req = DummyRequest({"x-correlation-id": invalid_id})
    extracted = CorrelationContext.extract_from_headers(req.headers)
    assert extracted is None


# A generated id is `corr_` and sixteen lowercase hex digits. The marker in the refused id below
# holds `x` and `q`, which are in neither the prefix nor the alphabet, so it cannot occur in one
# whatever the generator returns. It was `bad`, which is hex, and 53 of 20,000 generated ids held it.
REFUSED_ID = "xq\nzz"
GENERATED = re.compile(r"corr_[0-9a-f]{16}")


def test_invalid_correlation_id_in_decorator_rejected():
    svc = MyService()
    req = DummyRequest({"x-correlation-id": REFUSED_ID})
    cid = svc.do_work(req)
    assert cid != REFUSED_ID
    assert "xq" not in cid
    assert GENERATED.fullmatch(cid), cid


def test_CONTROL_the_refused_id_is_always_refused_and_never_a_substring_of_a_generated_one():
    assert refusal_of(REFUSED_ID) is not None
    ids = [CorrelationContext.new_id_unless_given(None) for _ in range(20_000)]
    assert all(GENERATED.fullmatch(i) for i in ids)
    assert not [i for i in ids if "xq" in i or "zz" in i]


def test_CONTROL_the_marker_this_test_used_to_assert_on_does_occur_in_generated_ids():
    """The reason for the change: `bad` is hex, so a correct generator produces it now and then."""
    ids = [CorrelationContext.new_id_unless_given(None) for _ in range(20_000)]
    assert any("bad" in i for i in ids)


def test_decorated_bound_method_ignores_self_headers():
    class ServiceWithHeaders:
        def __init__(self):
            self.headers = {"x-correlation-id": "service-header"}

        @with_correlation_id
        def do_work(self, req):
            return CorrelationContext.get()

    svc = ServiceWithHeaders()
    req = DummyRequest({"x-correlation-id": "request-header"})

    # Assert it adopts the request header, not the service header
    cid = svc.do_work(req)
    assert cid == "request-header"


def test_newline_rejection_specifically():
    assert CorrelationContext.new_id_unless_given("abc\n") != "abc\n"
    assert CorrelationContext.new_id_unless_given("abc\r\ndef") != "abc\r\ndef"
    assert CorrelationContext.new_id_unless_given("valid_id_123") == "valid_id_123"


pytestmark = pytest.mark.unit


def test_correlation_id_rejection_logging():
    from loguru import logger

    logs = []
    handler_id = logger.add(lambda msg: logs.append(msg), level="WARNING")

    try:
        # Test valid ID doesn't log
        CorrelationContext.get_or_create_id("valid_id_123")
        assert not logs

        CorrelationContext.get_or_create_id("🚀unicode_is_fine🚀")
        assert not logs

        # Test invalid ID logs
        CorrelationContext.get_or_create_id("bad\nboy")
        assert len(logs) == 1
        assert "Rejected invalid correlation ID" in logs[0]
        assert "WARNING" in logs[0]
    finally:
        logger.remove(handler_id)


class PositionalService:
    @with_correlation_id
    def do_work(self, request, correlation_id=None):
        return correlation_id

    @with_correlation_id
    async def do_work_async(self, request, correlation_id=None):
        return correlation_id

    @with_correlation_id
    def do_work_keyword_only(self, request, *, correlation_id=None):
        return correlation_id

    @with_correlation_id
    def do_work_payload(self, payload, correlation_id=None):
        return correlation_id

    @with_correlation_id
    async def do_work_payload_async(self, payload, correlation_id=None):
        return correlation_id


def test_a_positional_correlation_id_is_the_one_the_function_receives():
    req = DummyRequest({"x-correlation-id": "request-header"})

    assert PositionalService().do_work(req, "explicit-id") == "explicit-id"


@pytest.mark.asyncio
async def test_a_positional_correlation_id_is_the_one_the_function_receives_async():
    req = DummyRequest({"x-correlation-id": "request-header"})

    assert await PositionalService().do_work_async(req, "explicit-id") == "explicit-id"


def test_a_positional_none_falls_back_to_the_request_header():
    req = DummyRequest({"x-correlation-id": "request-header"})

    assert PositionalService().do_work(req, None) == "request-header"


def test_a_keyword_only_correlation_id_is_filled_from_the_request_header():
    req = DummyRequest({"x-correlation-id": "request-header"})

    assert PositionalService().do_work_keyword_only(req) == "request-header"


def test_a_dict_argument_carrying_a_correlation_id_supplies_it():
    payload = {"correlation_id": "from-dict"}

    assert PositionalService().do_work_payload(payload) == "from-dict"


@pytest.mark.asyncio
async def test_a_dict_argument_carrying_a_correlation_id_supplies_it_async():
    payload = {"correlation_id": "from-dict"}

    assert await PositionalService().do_work_payload_async(payload) == "from-dict"


def test_a_call_that_does_not_fit_the_signature_fails_in_the_function_itself():
    req = DummyRequest({"x-correlation-id": "request-header"})

    with pytest.raises(TypeError, match=r"positional arguments but 4 were given"):
        PositionalService().do_work(req, "explicit-id", "one-too-many")

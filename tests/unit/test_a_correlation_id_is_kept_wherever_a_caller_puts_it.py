"""A correlation id is kept wherever a caller puts it, and an id that is not a string is named.

`with_correlation_id` reads an id passed by keyword before it binds the call, so a function that
takes `**kwargs` runs under the id it was given: `bind_partial` files that keyword under the
`**kwargs` parameter, not under `correlation_id`. `extract_from_headers` drops a header whose value
is None before it folds the names to lower case, so a None under one spelling of a name cannot hide
the value under another. A payload id that is not a string is ignored with a warning naming its type.
"""

import asyncio

import pytest
from loguru import logger

from cliffracer.core.correlation import CorrelationContext, get_correlation_id, with_correlation_id

pytestmark = pytest.mark.unit


@pytest.fixture
def warnings():
    seen: list[str] = []
    sink = logger.add(
        lambda message: seen.append(str(message).rstrip("\n")), level="WARNING", format="{message}"
    )
    yield seen
    logger.remove(sink)


def test_a_function_taking_kwargs_runs_under_the_id_passed_by_keyword():
    @with_correlation_id
    def handle(**kwargs):
        return get_correlation_id(), kwargs

    assert handle(correlation_id="abc") == ("abc", {"correlation_id": "abc"})


def test_an_async_function_taking_kwargs_runs_under_the_id_passed_by_keyword():
    @with_correlation_id
    async def handle(x, **kwargs):
        return get_correlation_id(), kwargs

    assert asyncio.run(handle(1, correlation_id="abc")) == ("abc", {"correlation_id": "abc"})


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Correlation-ID": "abc", "x-correlation-id": None},
        {"x-correlation-id": None, "X-Correlation-ID": "abc"},
    ],
    ids=["none-after", "none-before"],
)
def test_a_none_header_under_another_spelling_does_not_hide_the_id(headers):
    assert CorrelationContext.extract_from_headers(headers) == "abc"


def test_a_payload_id_that_is_not_a_string_is_ignored_with_a_warning_naming_its_type(warnings):
    cid = CorrelationContext.for_message({}, {"correlation_id": 5})

    assert cid.startswith("corr_"), cid
    assert warnings == ["Ignored a correlation ID that is a int, not a string"]

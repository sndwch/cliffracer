"""ServiceConfig.name validation tests."""

import pytest
from pydantic import ValidationError

from cliffracer import ServiceConfig

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "valid_name", ["orders", "order_service", "orders-eu", "orders.eu", "svc1"]
)
def test_valid_service_name_accepted(valid_name):
    cfg = ServiceConfig(name=valid_name)
    assert cfg.name == valid_name


@pytest.mark.parametrize(
    ("bad_name", "expected_err"),
    [
        ("", "empty"),
        ("*", "wildcards"),
        (">", "wildcards"),
        ("orders.*", "wildcards"),
        ("orders.>", "wildcards"),
        ("has spaces", "whitespace"),
        (" ", "whitespace"),
        ("\t", "whitespace"),
        ("a\nb", "whitespace"),
        (".orders", "empty tokens"),
        ("orders.", "empty tokens"),
        ("orders..eu", "empty tokens"),
    ],
)
def test_invalid_service_name_rejected(bad_name, expected_err):
    with pytest.raises(ValidationError) as exc_info:
        ServiceConfig(name=bad_name)
    assert expected_err in str(exc_info.value)

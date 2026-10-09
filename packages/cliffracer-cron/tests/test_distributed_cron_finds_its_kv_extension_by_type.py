"""Distributed cron finds the service's KvExtension, whatever attribute name it was declared under."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cliffracer_cron import DistributedCronTimer, cron
from cliffracer_kv import KvExtension

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


def _service_declaring(**extensions: KvExtension) -> CliffracerService:
    """A service with a distributed cron handler and the given extensions, by attribute name."""
    body = {
        **extensions,
        "scheduled": cron("0 9 * * *", distributed=True)(lambda self: None),
    }
    return type("Svc", (CliffracerService,), body)(ServiceConfig(name="jobs"))


def _timer_of(service: CliffracerService) -> DistributedCronTimer:
    service.container.discover_handlers()
    (timer,) = service.container.registry.timers
    assert isinstance(timer, DistributedCronTimer)
    return timer


@pytest.mark.parametrize("attribute", ["kv", "store", "locks", "cron_kv"])
def test_a_kv_extension_is_found_under_any_attribute_name(attribute):
    service = _service_declaring(**{attribute: KvExtension()})

    timer = _timer_of(service)  # discovery accepts it

    assert timer._find_kv_extension(service) is getattr(service, attribute)


def test_two_kv_extensions_use_the_one_named_kv():
    service = _service_declaring(kv=KvExtension(), archive=KvExtension())

    assert _timer_of(service)._find_kv_extension(service) is service.kv


def test_two_kv_extensions_and_none_named_kv_are_refused_naming_both():
    with pytest.raises(ConfigurationError) as caught:
        _timer_of(_service_declaring(store=KvExtension(), archive=KvExtension()))

    message = str(caught.value)
    assert "'store'" in message and "'archive'" in message, message
    assert "none is named 'kv'" in message, message


def test_CONTROL_a_service_with_no_kv_extension_is_still_refused_at_discovery():
    service = _service_declaring()

    with pytest.raises(ConfigurationError, match="no KvExtension is registered"):
        service.container.discover_handlers()


def test_CONTROL_an_explicit_handle_wins_over_the_declared_extension():
    declared = KvExtension()
    handle = object()
    service = _service_declaring(store=declared)
    timer = DistributedCronTimer("0 9 * * *", kv_extension=handle)

    assert timer._find_kv_extension(service) is handle


def test_CONTROL_a_stand_in_named_kv_that_can_serve_a_bucket_is_still_accepted():
    """A double that is not a KvExtension needs no handle, as long as it can serve a bucket."""
    stand_in = SimpleNamespace(get_bucket=AsyncMock())
    service = SimpleNamespace(kv=stand_in, container=SimpleNamespace(extensions=[]))

    assert DistributedCronTimer("0 9 * * *")._find_kv_extension(service) is stand_in


def test_CONTROL_a_declared_stand_in_named_kv_that_can_serve_a_bucket_is_accepted():
    stand_in = SimpleNamespace(name="kv", get_bucket=AsyncMock())
    service = SimpleNamespace(container=SimpleNamespace(extensions=[stand_in]))

    assert DistributedCronTimer("0 9 * * *")._find_kv_extension(service) is stand_in


def test_something_named_kv_that_cannot_serve_a_bucket_is_not_taken_for_the_store():
    """A name is not a type: an unrelated attribute called `kv` is no KvExtension."""
    service = SimpleNamespace(kv=object(), container=SimpleNamespace(extensions=[]))

    assert DistributedCronTimer("0 9 * * *")._find_kv_extension(service) is None


def test_a_declared_extension_named_kv_that_cannot_serve_a_bucket_is_not_taken_for_the_store():
    unrelated = SimpleNamespace(name="kv")
    service = SimpleNamespace(container=SimpleNamespace(extensions=[unrelated]))

    assert DistributedCronTimer("0 9 * * *")._find_kv_extension(service) is None


def test_a_service_whose_kv_is_an_unrelated_extension_is_refused_before_it_starts():
    """Accepting it moved the failure to the first tick, an AttributeError on `get_bucket`."""

    class NotKv(Extension):
        name = "kv"

    class Svc(CliffracerService):
        kv = NotKv()
        scheduled = cron("0 9 * * *", distributed=True)(lambda self: None)

    with pytest.raises(ConfigurationError, match="no KvExtension is registered"):
        Svc(ServiceConfig(name="jobs")).container.discover_handlers()

"""`nc=` and `js=` given where the extension is declared reach every service as the same connection."""

import threading

import pytest
from cliffracer_kv import KvExtension

from cliffracer import CliffracerService, ServiceConfig, SharedDependency

pytestmark = pytest.mark.unit


class Connection:
    """Stands in for a live client: it holds a lock, so it cannot be copied, as a live one cannot."""

    def __init__(self) -> None:
        self.lock = threading.Lock()

    def jetstream(self) -> "Connection":
        return self


def _service(declared: KvExtension, name: str) -> CliffracerService:
    class Svc(CliffracerService):
        kv = declared

    return Svc(ServiceConfig(name=name))


def test_a_js_given_at_declaration_is_the_one_each_service_resolves():
    js = Connection()
    declared = KvExtension(js=js)

    first, second = _service(declared, "first"), _service(declared, "second")

    assert first.kv._resolve_js() is js
    assert second.kv._resolve_js() is js


def test_an_nc_given_at_declaration_is_the_one_each_service_builds_its_context_from():
    nc = Connection()
    declared = KvExtension(nc=nc)

    first, second = _service(declared, "first"), _service(declared, "second")

    assert first.kv._explicit_nc is nc
    assert second.kv._explicit_nc is nc
    assert first.kv._resolve_js() is nc  # `nc.jetstream()` hands back itself here


def test_a_connection_given_positionally_is_shared_too():
    nc = Connection()
    declared = KvExtension(None, None, None, None, True, nc)

    assert _service(declared, "s").kv._explicit_nc is nc


def test_the_explicit_shared_dependency_form_still_works():
    js = Connection()
    declared = KvExtension(js=SharedDependency(js))

    assert _service(declared, "s").kv._resolve_js() is js


def test_everything_else_a_declaration_carries_is_still_copied_per_service():
    """Sharing the connection must not turn declaration state into shared state."""
    declared = KvExtension(js=Connection(), bucket_ttls={"sessions": 60})

    first, second = _service(declared, "first"), _service(declared, "second")

    assert first.kv._init_bucket_ttls == {"sessions": 60}
    assert first.kv._init_bucket_ttls is not second.kv._init_bucket_ttls


def test_CONTROL_a_declaration_with_no_connection_still_resolves_from_its_service():
    declared = KvExtension()
    service = _service(declared, "s")
    service.js = Connection()

    assert service.kv._resolve_js() is service.js

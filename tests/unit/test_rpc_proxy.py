import pytest

pytestmark = pytest.mark.unit


def test_rpc_proxy_does_not_leak_service_instance():
    import gc
    import weakref

    from cliffracer import CliffracerService, ServiceConfig
    from cliffracer.rpc_proxy import RpcProxy

    class LeakTestService(CliffracerService):
        target = RpcProxy("target")

    svc = LeakTestService(ServiceConfig(name="test"))
    proxy = svc.target
    assert proxy is not None

    ref = weakref.ref(svc)
    del svc
    # Wait, proxy holds a strong reference to instance.
    # We must also delete `proxy` before gc.collect()!
    del proxy

    gc.collect()
    assert ref() is None

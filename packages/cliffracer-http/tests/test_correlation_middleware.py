"""Tests for HTTP correlation middleware and FastAPI correlation ID dependency."""

import pytest
from cliffracer_http import HttpExtension
from cliffracer_http.correlation_middleware import CorrelationMiddleware, correlation_id_dependency
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


class Svc(CliffracerService):
    http = HttpExtension(port=0)

    @http.get("/echo")
    async def echo(self) -> dict:
        return {"ok": True}


async def test_the_middleware_propagates_a_supplied_correlation_id():
    svc = Svc(ServiceConfig(name="http_test_service"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    client = TestClient(svc.http.app)

    response = client.get("/echo", headers={"X-Correlation-ID": "http_corr_123"})
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "http_corr_123"


async def test_the_middleware_mints_one_when_the_caller_sends_none():
    svc = Svc(ServiceConfig(name="http_test_service"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    client = TestClient(svc.http.app)

    response = client.get("/echo")
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"].startswith("corr_")


def test_the_fastapi_dependency_sees_the_middleware_s_id():
    app = FastAPI()
    captured = None

    @app.get("/test")
    async def endpoint(correlation_id: str = Depends(correlation_id_dependency)):
        nonlocal captured
        captured = correlation_id
        return {"correlation_id": correlation_id}

    app.add_middleware(CorrelationMiddleware)
    client = TestClient(app)

    response = client.get("/test", headers={"X-Correlation-ID": "dep_test_456"})
    assert response.status_code == 200
    assert captured == "dep_test_456"

"""Unit tests for Auto-Gateway dynamic RPC ingress."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from cliffracer_http import (
    AutoGateway,
    AutoGatewayExtension,
    HttpExtension,
    generate_route_path,
    infer_http_verb,
    mount_rpc_routes,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer.core.exceptions import RPCError, RPCTimeoutError
from cliffracer.core.extension import ExtensionSetupContext
from cliffracer.core.service_config import ServiceConfig
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Models and Service Fixtures
# ---------------------------------------------------------------------------


class User(BaseModel):
    id: str
    name: str
    email: str


class CreateUserRequest(BaseModel):
    name: str
    email: str


class UpdateUserRequest(BaseModel):
    name: str | None = None
    email: str | None = None


class SampleUserService(CliffracerService):
    SERVICE = "users"
    VERSION = "1.0.0"

    @rpc
    async def get_user(self, user_id: str) -> User:
        """Fetch user by id."""
        return User(id=user_id, name="Alice", email="alice@test.local")

    @rpc
    async def list_users(self, limit: int = 10, offset: int = 0) -> list[User]:
        """List users with pagination."""
        return [User(id="1", name="Alice", email="alice@test.local")]

    @rpc
    async def create_user(self, request: CreateUserRequest) -> User:
        """Create a new user."""
        return User(id="new_1", name=request.name, email=request.email)

    @rpc
    async def update_user(self, user_id: str, request: UpdateUserRequest) -> User:
        """Update an existing user."""
        return User(
            id=user_id, name=request.name or "Alice", email=request.email or "alice@test.local"
        )

    @rpc
    async def patch_user_email(self, user_id: str, email: str) -> User:
        """Patch user email."""
        return User(id=user_id, name="Alice", email=email)

    @rpc
    async def delete_user(self, user_id: str) -> bool:
        """Delete user by id."""
        return True

    @rpc
    async def calculate_score(self, base: int, multiplier: int = 2) -> int:
        """Calculate score with default POST verb."""
        return base * multiplier


# ---------------------------------------------------------------------------
# Verb Inference Tests
# ---------------------------------------------------------------------------


def test_infer_http_verb_prefixes():
    """Verify prefix-based HTTP verb inference."""
    # GET prefixes
    assert infer_http_verb("get_user") == "GET"
    assert infer_http_verb("list_items") == "GET"
    assert infer_http_verb("fetch_record") == "GET"
    assert infer_http_verb("find_by_id") == "GET"
    assert infer_http_verb("read_file") == "GET"
    assert infer_http_verb("search_query") == "GET"
    assert infer_http_verb("query_status") == "GET"

    # POST prefixes
    assert infer_http_verb("create_order") == "POST"
    assert infer_http_verb("add_item") == "POST"
    assert infer_http_verb("post_message") == "POST"
    assert infer_http_verb("insert_row") == "POST"
    assert infer_http_verb("register_device") == "POST"
    assert infer_http_verb("new_session") == "POST"

    # PUT prefixes
    assert infer_http_verb("update_profile") == "PUT"
    assert infer_http_verb("set_preference") == "PUT"
    assert infer_http_verb("put_value") == "PUT"
    assert infer_http_verb("modify_account") == "PUT"
    assert infer_http_verb("replace_record") == "PUT"

    # PATCH prefix
    assert infer_http_verb("patch_status") == "PATCH"

    # DELETE prefixes
    assert infer_http_verb("delete_order") == "DELETE"
    assert infer_http_verb("remove_item") == "DELETE"
    assert infer_http_verb("drop_table") == "DELETE"
    assert infer_http_verb("clear_cache") == "DELETE"
    assert infer_http_verb("cancel_job") == "DELETE"

    # Default POST for unrecognized prefixes
    assert infer_http_verb("calculate_tax") == "POST"
    assert infer_http_verb("process_transaction") == "POST"
    assert infer_http_verb("run_migration") == "POST"
    assert infer_http_verb("do_something") == "POST"


def test_infer_http_verb_overrides():
    """Verify that explicit overrides take precedence over prefix inference."""
    overrides = {
        "get_export": "POST",
        "create_temporary": "PUT",
        "update_status": "PATCH",
        "do_action": "GET",
    }
    assert infer_http_verb("get_export", overrides=overrides) == "POST"
    assert infer_http_verb("create_temporary", overrides=overrides) == "PUT"
    assert infer_http_verb("update_status", overrides=overrides) == "PATCH"
    assert infer_http_verb("do_action", overrides=overrides) == "GET"
    # Unoverridden should still follow prefix rules
    assert infer_http_verb("get_user", overrides=overrides) == "GET"


# ---------------------------------------------------------------------------
# Route Path Generation Tests
# ---------------------------------------------------------------------------


def test_generate_route_path():
    """Verify route path formatting with prefixes and overrides."""
    # Standard path
    assert generate_route_path("users", "get_user") == "/users/get_user"
    assert generate_route_path("users", "create_user", prefix="/api") == "/api/users/create_user"
    assert (
        generate_route_path("users", "create_user", prefix="api/v1") == "/api/v1/users/create_user"
    )

    # Path overrides
    overrides = {"get_user": "/users/{user_id}"}
    assert generate_route_path("users", "get_user", path_overrides=overrides) == "/users/{user_id}"
    assert (
        generate_route_path("users", "get_user", prefix="/api", path_overrides=overrides)
        == "/api/users/{user_id}"
    )

    # Path override that already contains prefix
    overrides_prefixed = {"get_user": "/api/users/{user_id}"}
    assert (
        generate_route_path("users", "get_user", prefix="/api", path_overrides=overrides_prefixed)
        == "/api/users/{user_id}"
    )


# ---------------------------------------------------------------------------
# Route Mounting and Ingress Execution Tests
# ---------------------------------------------------------------------------


def test_mount_routes_from_service_class():
    """Verify mounting routes from a CliffracerService class."""
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock(return_value={"id": "1", "name": "Alice", "email": "a@b.com"})

    routes = mount_rpc_routes(
        app=app,
        service=mock_service,
        target=SampleUserService,
        prefix="/api",
    )

    expected_routes = [
        "/api/users/calculate_score",
        "/api/users/create_user",
        "/api/users/delete_user",
        "/api/users/get_user",
        "/api/users/list_users",
        "/api/users/patch_user_email",
        "/api/users/update_user",
    ]
    assert sorted(routes) == sorted(expected_routes)

    # Verify registered route methods
    registered = {r.path: list(r.methods)[0] for r in app.routes if hasattr(r, "methods")}
    assert registered["/api/users/get_user"] == "GET"
    assert registered["/api/users/list_users"] == "GET"
    assert registered["/api/users/create_user"] == "POST"
    assert registered["/api/users/update_user"] == "PUT"
    assert registered["/api/users/patch_user_email"] == "PATCH"
    assert registered["/api/users/delete_user"] == "DELETE"
    assert registered["/api/users/calculate_score"] == "POST"


def test_mount_routes_from_description():
    """Verify mounting routes directly from an introspect Description."""
    desc = describe(SampleUserService, service="users_v1", version="1.0.0")
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock()

    routes = mount_rpc_routes(app=app, service=mock_service, target=desc, prefix="/v1")
    assert "/v1/users_v1/get_user" in routes
    assert "/v1/users_v1/create_user" in routes


def test_get_endpoint_query_parameters():
    """Verify GET endpoint extracts query parameters and executes call_rpc."""
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock(
        return_value={"id": "u42", "name": "Bob", "email": "bob@example.com"}
    )

    mount_rpc_routes(app=app, service=mock_service, target=SampleUserService, prefix="/api")
    client = TestClient(app)

    res = client.get("/api/users/get_user?user_id=u42")
    assert res.status_code == 200
    assert res.json() == {"id": "u42", "name": "Bob", "email": "bob@example.com"}

    mock_service.call_rpc.assert_awaited_once_with("users", "get_user", user_id="u42")


def test_get_endpoint_default_query_parameters():
    """Verify GET endpoint handles default values for query parameters."""
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock(return_value=[])

    mount_rpc_routes(app=app, service=mock_service, target=SampleUserService, prefix="/api")
    client = TestClient(app)

    # Default parameters: limit=10, offset=0
    res1 = client.get("/api/users/list_users")
    assert res1.status_code == 200
    mock_service.call_rpc.assert_awaited_with("users", "list_users", limit=10, offset=0)

    # Override limit
    res2 = client.get("/api/users/list_users?limit=25&offset=50")
    assert res2.status_code == 200
    mock_service.call_rpc.assert_awaited_with("users", "list_users", limit=25, offset=50)


def test_post_endpoint_pydantic_body():
    """Verify POST endpoint validates JSON body and converts Pydantic model to kwargs."""
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock(
        return_value={"id": "new_123", "name": "Charlie", "email": "charlie@example.com"}
    )

    mount_rpc_routes(app=app, service=mock_service, target=SampleUserService, prefix="/api")
    client = TestClient(app)

    res = client.post(
        "/api/users/create_user",
        json={"name": "Charlie", "email": "charlie@example.com"},
    )
    assert res.status_code == 200
    assert res.json() == {"id": "new_123", "name": "Charlie", "email": "charlie@example.com"}

    mock_service.call_rpc.assert_awaited_once_with(
        "users",
        "create_user",
        request={"name": "Charlie", "email": "charlie@example.com"},
    )


def test_post_endpoint_multi_scalar_body():
    """Verify POST endpoint with multiple scalar parameters embeds into body."""
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock(return_value=200)

    mount_rpc_routes(app=app, service=mock_service, target=SampleUserService, prefix="/api")
    client = TestClient(app)

    res = client.post("/api/users/calculate_score", json={"base": 100, "multiplier": 2})
    assert res.status_code == 200
    assert res.json() == 200
    mock_service.call_rpc.assert_awaited_once_with(
        "users", "calculate_score", base=100, multiplier=2
    )


def test_path_parameter_binding():
    """Verify path parameter in path_overrides binds URL path segment."""
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock(
        return_value={"id": "u99", "name": "Alice", "email": "alice@test.local"}
    )

    mount_rpc_routes(
        app=app,
        service=mock_service,
        target=SampleUserService,
        prefix="/api",
        path_overrides={"get_user": "/users/{user_id}"},
    )
    client = TestClient(app)

    res = client.get("/api/users/u99")
    assert res.status_code == 200
    mock_service.call_rpc.assert_awaited_once_with("users", "get_user", user_id="u99")


# ---------------------------------------------------------------------------
# Error Translation Tests
# ---------------------------------------------------------------------------


def test_downstream_timeout_translates_to_504():
    """Downstream RPCTimeoutError translates to HTTP 504 Gateway Timeout."""
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock(side_effect=RPCTimeoutError("Downstream service timed out"))

    mount_rpc_routes(app=app, service=mock_service, target=SampleUserService)
    client = TestClient(app)

    res = client.get("/users/get_user?user_id=123")
    assert res.status_code == 504
    assert "Gateway timeout calling users.get_user" in res.json()["detail"]


def test_downstream_validation_error_translates_to_422():
    """Downstream RPCError with details translates to HTTP 422 Unprocessable Entity."""
    app = FastAPI()
    mock_service = MagicMock()
    details = [{"loc": ["email"], "msg": "Invalid email address format"}]
    mock_service.call_rpc = AsyncMock(side_effect=RPCError("Validation failure", details=details))

    mount_rpc_routes(app=app, service=mock_service, target=SampleUserService)
    client = TestClient(app)

    res = client.get("/users/get_user?user_id=123")
    assert res.status_code == 422
    assert res.json()["detail"]["details"] == details


def test_downstream_generic_rpc_error_translates_to_502():
    """Generic downstream RPCError translates to HTTP 502 Bad Gateway."""
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock(side_effect=RPCError("No responders for subject"))

    mount_rpc_routes(app=app, service=mock_service, target=SampleUserService)
    client = TestClient(app)

    res = client.get("/users/get_user?user_id=123")
    assert res.status_code == 502
    assert "RPC error calling users.get_user" in res.json()["detail"]


def test_unexpected_downstream_exception_translates_to_500():
    """Unexpected exception translates to HTTP 500 Internal Server Error."""
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock(side_effect=RuntimeError("Database connection lost"))

    mount_rpc_routes(app=app, service=mock_service, target=SampleUserService)
    client = TestClient(app)

    res = client.get("/users/get_user?user_id=123")
    assert res.status_code == 500
    assert "Internal gateway error" in res.json()["detail"]


# ---------------------------------------------------------------------------
# OpenAPI Schema Generation Tests
# ---------------------------------------------------------------------------


def test_openapi_schema_generation():
    """Verify OpenAPI documentation is automatically produced with schemas and response models."""
    app = FastAPI(title="Gateway Test API", version="2.0.0")
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock()

    mount_rpc_routes(app=app, service=mock_service, target=SampleUserService, prefix="/api")

    openapi = app.openapi()
    assert openapi["info"]["title"] == "Gateway Test API"

    # Paths present
    paths = openapi["paths"]
    assert "/api/users/get_user" in paths
    assert "/api/users/create_user" in paths
    assert "/api/users/list_users" in paths

    # GET endpoint parameters
    get_params = paths[
        "api/users/get_user" if "api/users/get_user" in paths else "/api/users/get_user"
    ]["get"]["parameters"]
    assert any(p["name"] == "user_id" and p["in"] == "query" for p in get_params)

    # POST endpoint requestBody
    post_route = paths["/api/users/create_user"]["post"]
    assert "requestBody" in post_route
    assert "$ref" in post_route["requestBody"]["content"]["application/json"]["schema"]

    # 200 response models
    get_200 = paths["/api/users/get_user"]["get"]["responses"]["200"]
    assert "$ref" in get_200["content"]["application/json"]["schema"]

    # Component schemas registered
    components = openapi["components"]["schemas"]
    assert "User" in components
    assert "CreateUserRequest" in components


# ---------------------------------------------------------------------------
# AutoGateway Manager Tests
# ---------------------------------------------------------------------------


def test_auto_gateway_manager():
    """Verify AutoGateway manages mounting and route tracking."""
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock(return_value={"id": "1", "name": "A", "email": "a@b.com"})

    gateway = AutoGateway(service=mock_service, prefix="/api")
    routes = gateway.mount(SampleUserService)

    assert len(routes) == 7
    assert gateway.mounted_routes == routes

    client = TestClient(gateway.app)
    res = client.get("/api/users/get_user?user_id=1")
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# AutoGatewayExtension Lifecycle Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_gateway_extension_lifecycle_with_http_extension():
    """Verify AutoGatewayExtension coordinates with HttpExtension on a service."""

    class GatewayService(CliffracerService):
        http = HttpExtension(port=9090)
        gateway = AutoGatewayExtension(targets=[SampleUserService], prefix="/api")

        def __init__(self, config):
            super().__init__(config)
            self.call_rpc = AsyncMock(
                return_value={"id": "1", "name": "Alice", "email": "alice@test.local"}
            )

    config = ServiceConfig(name="gateway_svc", nats_url="nats://localhost:4222")
    service = GatewayService(config)
    ctx = ExtensionSetupContext(
        service_config=config, broker_url="nats://localhost:4222", service=service
    )

    # Run extension setup
    await service.http.setup(ctx)
    await service.gateway.setup(ctx)

    # Routes should be mounted on the shared http.app
    assert service.gateway.app is service.http.app
    assert len(service.gateway.mounted_routes) == 7

    # Lifecycle start and stop
    await service.gateway.start()
    await service.gateway.stop()

    # Test invoking via test client
    client = TestClient(service.http.app)
    res = client.get("/api/users/get_user?user_id=usr_77")
    assert res.status_code == 200
    service.call_rpc.assert_awaited_once_with("users", "get_user", user_id="usr_77")


@pytest.mark.asyncio
async def test_auto_gateway_extension_standalone():
    """Verify AutoGatewayExtension works standalone without HttpExtension."""

    class StandaloneService(CliffracerService):
        gateway = AutoGatewayExtension(targets=[SampleUserService], prefix="/v1")

        def __init__(self, config):
            super().__init__(config)
            self.call_rpc = AsyncMock(
                return_value={"id": "1", "name": "Alice", "email": "alice@test.local"}
            )

    config = ServiceConfig(name="standalone_svc", nats_url="nats://localhost:4222")
    service = StandaloneService(config)
    ctx = ExtensionSetupContext(
        service_config=config, broker_url="nats://localhost:4222", service=service
    )

    await service.gateway.setup(ctx)
    assert service.gateway.app is not None
    assert len(service.gateway.mounted_routes) == 7

    client = TestClient(service.gateway.app)
    res = client.get("/v1/users/get_user?user_id=123")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_http_extension_mount_rpc_service_helper():
    """Verify HttpExtension.mount_rpc_service helper method."""

    class ServiceWithHttp(CliffracerService):
        http = HttpExtension()

        def __init__(self, config):
            super().__init__(config)
            self.call_rpc = AsyncMock(
                return_value={"id": "1", "name": "Alice", "email": "alice@test.local"}
            )

    config = ServiceConfig(name="my_http_svc", nats_url="nats://localhost:4222")
    service = ServiceWithHttp(config)
    ctx = ExtensionSetupContext(
        service_config=config, broker_url="nats://localhost:4222", service=service
    )

    await service.http.setup(ctx)
    routes = service.http.mount_rpc_service(SampleUserService, prefix="/rpc")
    assert len(routes) == 7

    client = TestClient(service.http.app)
    res = client.get("/rpc/users/get_user?user_id=1")
    assert res.status_code == 200


def test_include_and_exclude_methods():
    """Verify include_methods and exclude_methods filtering."""
    app = FastAPI()
    mock_service = MagicMock()
    mock_service.call_rpc = AsyncMock()

    # Include only get_user
    routes1 = mount_rpc_routes(
        app=app,
        service=mock_service,
        target=SampleUserService,
        include_methods=["get_user"],
    )
    assert routes1 == ["/users/get_user"]

    # Exclude delete_user and calculate_score
    app2 = FastAPI()
    routes2 = mount_rpc_routes(
        app=app2,
        service=mock_service,
        target=SampleUserService,
        exclude_methods=["delete_user", "calculate_score"],
    )
    assert "/users/delete_user" not in routes2
    assert "/users/calculate_score" not in routes2
    assert "/users/get_user" in routes2
    assert "/users/create_user" in routes2


@pytest.mark.asyncio
async def test_auto_gateway_extension_auto_mounts_host_service():
    """Verify AutoGatewayExtension with targets=None automatically mounts hosting service's RPC methods."""

    class SelfMountedUserService(CliffracerService):
        SERVICE = "users"
        http = HttpExtension(port=8080)
        gateway = AutoGatewayExtension(prefix="/api/v1")

        def __init__(self, config):
            super().__init__(config)
            self.call_rpc = AsyncMock(
                return_value={"id": "u1", "name": "Alice", "email": "alice@test.local"}
            )

        @rpc
        async def get_user(self, user_id: str) -> User:
            return User(id=user_id, name="Alice", email="alice@test.local")

        @rpc
        async def create_user(self, request: CreateUserRequest) -> User:
            return User(id="new_1", name=request.name, email=request.email)

        @rpc
        async def delete_user(self, user_id: str) -> bool:
            return True

    config = ServiceConfig(name="users", nats_url="nats://localhost:4222")
    service = SelfMountedUserService(config)
    ctx = ExtensionSetupContext(
        service_config=config, broker_url="nats://localhost:4222", service=service
    )

    await service.http.setup(ctx)
    await service.gateway.setup(ctx)

    assert service.gateway.app is service.http.app
    assert "/api/v1/users/get_user" in service.gateway.mounted_routes
    assert "/api/v1/users/create_user" in service.gateway.mounted_routes
    assert "/api/v1/users/delete_user" in service.gateway.mounted_routes

    client = TestClient(service.http.app)
    res = client.get("/api/v1/users/get_user?user_id=123")
    assert res.status_code == 200
    service.call_rpc.assert_awaited_once_with("users", "get_user", user_id="123")


@pytest.mark.asyncio
async def test_auto_gateway_extension_services_alias():
    """Verify services argument can be used as an alias for targets."""
    gateway = AutoGatewayExtension(services=[SampleUserService], prefix="/v2")
    config = ServiceConfig(name="alias_svc", nats_url="nats://localhost:4222")
    service = CliffracerService(config)
    ctx = ExtensionSetupContext(
        service_config=config, broker_url="nats://localhost:4222", service=service
    )

    await gateway.setup(ctx)
    assert len(gateway.mounted_routes) == 7
    assert "/v2/users/get_user" in gateway.mounted_routes

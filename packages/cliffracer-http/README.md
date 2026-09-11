# cliffracer-http

HTTP routes and websockets for cliffracer services, on FastAPI.

Core serves `GET /health` and `GET /info` from an asyncio listener. This package
is for services that also serve routes of their own.

```python
from cliffracer import CliffracerService, rpc
from pydantic import BaseModel

from cliffracer_http import HttpExtension


class Order(BaseModel):
    sku: str
    qty: int


class OrderService(CliffracerService):
    http = HttpExtension()

    @rpc
    async def create(self, order: Order) -> str: ...

    @http.get("/orders/{order_id}")
    async def get_order(self, order_id: str) -> dict[str, str]: ...
```

The extension serves `/health` and `/info` on its own FastAPI app as well.

## Ports

Declaring this extension gives the service one port: the extension's. It serves
your routes, `/health` and `/info`, and the core listener does not bind.

`HttpExtension` listens on 8000. Set the port with `HttpExtension(port=8080)`
or with `CLIFFRACER_HTTP_PORT`. `CLIFFRACER_HTTP_HOST` sets the bind address,
which defaults to `0.0.0.0`. `ServiceConfig.health_port` is what the core
listener uses on a service that does not declare this extension.

Point probes at the extension's port.

## Auto-Gateway for Dynamic RPC Ingress

`AutoGatewayExtension` dynamically mounts FastAPI HTTP endpoints backed by Cliffracer
RPC services, automatically inferring HTTP verbs, parsing parameters, dispatching NATS
RPC calls, translating error responses, and generating interactive OpenAPI docs.

### Example Service Definition

```python
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer_http import AutoGatewayExtension, HttpExtension


class UserModel(BaseModel):
    id: str
    name: str
    email: str


class CreateUserPayload(BaseModel):
    name: str
    email: str


class UserService(CliffracerService):
    name = "users"
    http = HttpExtension(port=8080)
    gateway = AutoGatewayExtension(prefix="/api/v1")

    @rpc
    async def get_user(self, user_id: str) -> UserModel:
        """Fetch user by id."""
        return UserModel(id=user_id, name="Alice", email="alice@example.com")

    @rpc
    async def create_user(self, payload: CreateUserPayload) -> UserModel:
        """Create new user."""
        return UserModel(id="usr_1", name=payload.name, email=payload.email)

    @rpc
    async def delete_user(self, user_id: str) -> bool:
        """Delete user by id."""
        return True
```

### Verb and Route Mapping

Method name prefixes map to standard HTTP verbs:

- `get_user` -> `GET /api/v1/users/get_user?user_id=...` (query parameters)
- `create_user` -> `POST /api/v1/users/create_user` (JSON request body `CreateUserPayload`)
- `delete_user` -> `DELETE /api/v1/users/delete_user?user_id=...` (query parameters)

The gateway prefixes routes with `/{prefix}/{service_name}/{method_name}` and infers:
- `GET`: `get_`, `list_`, `fetch_`, `find_`, `read_`, `search_`, `query_`
- `POST`: `create_`, `add_`, `post_`, `insert_`, `register_`, `new_` (and default for unrecognized prefixes)
- `PUT`: `update_`, `set_`, `put_`, `modify_`, `replace_`
- `PATCH`: `patch_`
- `DELETE`: `delete_`, `remove_`, `drop_`, `clear_`, `cancel_`

### Dedicated Gateway

A dedicated gateway service can also front downstream services by passing service classes to `targets`:

```python
from cliffracer import CliffracerService
from cliffracer_http import AutoGatewayExtension, HttpExtension


class GatewayService(CliffracerService):
    name = "gateway"
    http = HttpExtension(port=8080)
    gateway = AutoGatewayExtension(
        targets=[UserService],
        prefix="/api/v1",
    )
```

### Swagger and OpenAPI Documentation

When running, FastAPI serves interactive documentation:
- Swagger UI: `http://localhost:8080/docs`
- OpenAPI JSON Schema: `http://localhost:8080/openapi.json`

### Error Status Codes

- `200 OK`: Successful RPC response.
- `422 Unprocessable Entity`: Validation failure or downstream `RPCError` containing error details.
- `502 Bad Gateway`: Downstream RPC failure.
- `504 Gateway Timeout`: Downstream service timed out (`RPCTimeoutError`).
- `500 Internal Server Error`: Unhandled gateway exception.

Installed from PyPI, versioned in lockstep with `cliffracer`.

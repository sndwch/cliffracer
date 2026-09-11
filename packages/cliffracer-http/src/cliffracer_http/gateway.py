"""FastAPI route generation from RPC handler specifications.

Mounts FastAPI routes mapped directly to downstream RPC signatures,
inferring HTTP verbs, parsing path/query/body parameters, executing
`service.call_rpc(...)`, translating errors to standard HTTP status codes,
and generating full OpenAPI documentation with Pydantic schemas.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast, get_origin

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Path, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from cliffracer.core.correlation import with_correlation_id
from cliffracer.core.exceptions import (
    RpcError,
    RpcTimeoutError,
    RpcValidationError,
)
from cliffracer.core.extension import Extension, ExtensionSetupContext
from cliffracer.core.typed_rpc import HandlerSpec, python_type
from cliffracer.introspect import Description, Method, Param, describe

if TYPE_CHECKING:
    from .extension import HttpExtension

VERB_PREFIXES: list[tuple[tuple[str, ...], str]] = [
    (("get_", "list_", "fetch_", "find_", "read_", "search_", "query_"), "GET"),
    (("create_", "add_", "post_", "insert_", "register_", "new_"), "POST"),
    (("update_", "set_", "put_", "modify_", "replace_"), "PUT"),
    (("patch_",), "PATCH"),
    (("delete_", "remove_", "drop_", "clear_", "cancel_"), "DELETE"),
]


def infer_http_verb(method_name: str, overrides: dict[str, str] | None = None) -> str:
    """Infer HTTP verb from method name prefix or return override.

    Rules:
      get_, list_, fetch_, find_, read_, search_, query_ -> GET
      create_, add_, post_, insert_, register_, new_     -> POST
      update_, set_, put_, modify_, replace_             -> PUT
      patch_                                             -> PATCH
      delete_, remove_, drop_, clear_, cancel_           -> DELETE
      Default                                            -> POST
    """
    if overrides and method_name in overrides:
        return overrides[method_name].upper()

    lower = method_name.lower()
    for prefixes, verb in VERB_PREFIXES:
        if any(lower.startswith(p) for p in prefixes):
            return verb
    return "POST"


def generate_route_path(
    service_name: str,
    method_name: str,
    prefix: str = "",
    path_overrides: dict[str, str] | None = None,
) -> str:
    """Generate the URL path for an RPC method.

    If method_name is present in path_overrides, that path is used (with optional prefix).
    Otherwise, constructs /{prefix}/{service_name}/{method_name}.
    """
    if path_overrides and method_name in path_overrides:
        path = path_overrides[method_name]
        if not path.startswith("/"):
            path = "/" + path
        if prefix:
            clean_prefix = "/" + prefix.strip("/")
            if not path.startswith(clean_prefix + "/") and path != clean_prefix:
                path = f"{clean_prefix}{path}"
        return path

    clean_prefix = "/" + prefix.strip("/") if prefix and prefix.strip("/") else ""
    srv_clean = service_name.strip("/")
    method_clean = method_name.strip("/")
    parts = [p for p in (srv_clean, method_clean) if p]
    base = "/" + "/".join(parts)
    if clean_prefix:
        return f"{clean_prefix}{base}"
    return base


def _resolve_type(ref_or_tp: Any) -> Any:
    """Resolve a TypeRef dict or type hint into a Python type."""
    if isinstance(ref_or_tp, dict):
        try:
            return python_type(ref_or_tp)
        except Exception as exc:
            logger.debug(f"Failed to resolve TypeRef {ref_or_tp}: {exc}")
            return Any
    return ref_or_tp


def _normalize_method(spec: Method | HandlerSpec | Any) -> Method:
    """Normalize a Method or HandlerSpec into a Method dataclass."""
    if isinstance(spec, Method):
        return spec
    if isinstance(spec, HandlerSpec):
        params = [
            Param(
                name=p.name,
                type=p.ref,
                has_default=p.has_default,
                default=p.default,
            )
            for p in spec.params
        ]
        return Method(
            name=spec.name,
            doc=spec.doc,
            params=params,
            returns=spec.return_ref,
        )
    raise TypeError(f"Expected Method or HandlerSpec, got {type(spec).__name__}")


def create_rpc_endpoint(
    service: Any,
    target_name: str,
    method_spec: Method | HandlerSpec,
    verb: str,
    route_path: str,
    response_model: Any = None,
) -> Callable:
    """Synthesize a dynamic FastAPI endpoint bridging to service.call_rpc(...)."""
    method = _normalize_method(method_spec)
    path_param_names = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", route_path))

    param_types: dict[str, Any] = {}
    for p in method.params:
        param_types[p.name] = _resolve_type(p.type)

    path_params = [p for p in method.params if p.name in path_param_names]
    non_path_params = [p for p in method.params if p.name not in path_param_names]
    is_body_verb = verb in ("POST", "PUT", "PATCH")

    defaults_map: dict[str, Any] = {}
    for p in path_params:
        default_val = ... if not p.has_default else p.default
        defaults_map[p.name] = Path(default_val)

    if not is_body_verb:
        for p in non_path_params:
            default_val = ... if not p.has_default else p.default
            defaults_map[p.name] = Query(default_val)
    else:
        if len(non_path_params) == 1:
            p = non_path_params[0]
            py_tp = param_types[p.name]
            is_model = isinstance(py_tp, type) and issubclass(py_tp, BaseModel)
            is_dict = py_tp is dict or get_origin(py_tp) is dict
            default_val = ... if not p.has_default else p.default
            defaults_map[p.name] = Body(default_val, embed=not (is_model or is_dict))
        else:
            for p in non_path_params:
                default_val = ... if not p.has_default else p.default
                defaults_map[p.name] = Body(default_val)

    sig_params = [
        inspect.Parameter(
            name=p.name,
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=defaults_map[p.name],
            annotation=param_types[p.name],
        )
        for p in method.params
    ]
    annotations = {p.name: param_types[p.name] for p in method.params}
    if response_model is not None:
        annotations["return"] = response_model

    async def endpoint(**kwargs: Any) -> Any:
        rpc_kwargs: dict[str, Any] = {}
        for k, v in kwargs.items():
            if isinstance(v, BaseModel):
                rpc_kwargs[k] = v.model_dump(mode="json")
            else:
                rpc_kwargs[k] = v

        try:
            if hasattr(service, "call_rpc"):
                return await service.call_rpc(target_name, method.name, **rpc_kwargs)
            if callable(service):
                return await service(target_name, method.name, **rpc_kwargs)
            raise RuntimeError(f"Service {service!r} does not support call_rpc")
        except RpcTimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail=f"Gateway timeout calling {target_name}.{method.name}: {exc}",
            ) from exc
        except RpcValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": str(exc), "details": exc.details},
            ) from exc
        except RpcError as exc:
            details = getattr(exc, "details", None)
            if details:
                raise HTTPException(
                    status_code=422,
                    detail={"error": str(exc), "details": details},
                ) from exc
            raise HTTPException(
                status_code=502,
                detail=f"RPC error calling {target_name}.{method.name}: {exc}",
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Internal gateway error calling {target_name}.{method.name}: {exc}",
            ) from exc

    endpoint.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    endpoint.__annotations__ = annotations
    endpoint.__name__ = f"{target_name}_{method.name}"
    endpoint.__doc__ = method.doc

    return endpoint


def mount_rpc_routes(
    app: FastAPI,
    service: Any = None,
    target: Any = None,
    prefix: str = "",
    verb_overrides: dict[str, str] | None = None,
    path_overrides: dict[str, str] | None = None,
    description: Description | None = None,
    service_name: str | None = None,
    tags: list[str | Enum] | None = None,
    include_methods: list[str] | None = None,
    exclude_methods: list[str] | None = None,
    **extra_route_kwargs: Any,
) -> list[str]:
    """Programmatically register FastAPI endpoints for an RPC service.

    Discovers methods from service class, service instance, or Description.
    Returns the list of mounted route paths.
    """
    target_name: str
    methods: list[Method]

    if isinstance(target, Description):
        target_name = service_name or target.service
        methods = target.methods
    elif isinstance(target, type):
        target_name = (
            service_name
            or getattr(target, "SERVICE", None)
            or getattr(target, "name", None)
            or target.__name__.lower()
        )
        version = getattr(target, "VERSION", "1.0.0")
        desc = describe(target, service=target_name, version=version)
        methods = desc.methods
    elif isinstance(target, str):
        target_name = service_name or target
        if description is not None:
            methods = description.methods
        else:
            raise ValueError(
                f"Cannot discover methods for service name {target!r} without description"
            )
    elif target is not None:
        cls = type(target)
        config = getattr(target, "config", None)
        target_name = (
            service_name
            or getattr(config, "name", None)
            or getattr(target, "SERVICE", None)
            or getattr(target, "name", None)
            or cls.__name__.lower()
        )
        version = getattr(config, "version", "1.0.0")
        desc = describe(cls, service=target_name, version=version)
        methods = desc.methods
        if service is None and hasattr(target, "call_rpc"):
            service = target
    else:
        raise ValueError("Must provide target (class, instance, Description, or service name)")

    if service is None:
        raise ValueError("Must provide service to execute call_rpc")

    mounted_paths: list[str] = []
    target_tags: list[str | Enum] = list(tags) if tags is not None else [target_name]

    for method in methods:
        if method.name.startswith("_"):
            continue
        if include_methods is not None and method.name not in include_methods:
            continue
        if exclude_methods is not None and method.name in exclude_methods:
            continue

        verb = infer_http_verb(method.name, overrides=verb_overrides)
        route_path = generate_route_path(
            target_name,
            method.name,
            prefix=prefix,
            path_overrides=path_overrides,
        )

        response_model = None
        if method.returns:
            try:
                ret_tp = _resolve_type(method.returns)
                if ret_tp is not type(None):
                    response_model = ret_tp
            except Exception as exc:
                logger.debug(f"Could not resolve response model for {method.name}: {exc}")

        endpoint = create_rpc_endpoint(
            service=service,
            target_name=target_name,
            method_spec=method,
            verb=verb,
            route_path=route_path,
            response_model=response_model,
        )

        app.add_api_route(
            route_path,
            endpoint,
            methods=[verb],
            response_model=response_model,
            tags=target_tags,
            summary=method.name.replace("_", " ").title(),
            description=method.doc,
            **extra_route_kwargs,
        )
        mounted_paths.append(route_path)
        logger.debug(f"Mounted RPC route {verb} {route_path} -> {target_name}.{method.name}")

    return mounted_paths


class AutoGateway:
    """Gateway manager for dynamic RPC ingress."""

    def __init__(
        self,
        app: FastAPI | None = None,
        service: Any = None,
        prefix: str = "",
        **fastapi_kwargs: Any,
    ):
        self.app = app if app is not None else FastAPI(**fastapi_kwargs)
        self.service = service
        self.prefix = prefix
        self._mounted_routes: list[str] = []
        self._mounted_targets: dict[str, Any] = {}

    @property
    def mounted_routes(self) -> list[str]:
        return list(self._mounted_routes)

    def mount(
        self,
        target: Any,
        service: Any = None,
        prefix: str | None = None,
        verb_overrides: dict[str, str] | None = None,
        path_overrides: dict[str, str] | None = None,
        description: Description | None = None,
        service_name: str | None = None,
        tags: list[str | Enum] | None = None,
        **route_kwargs: Any,
    ) -> list[str]:
        """Mount an RPC target's methods onto this gateway's FastAPI app."""
        svc = service if service is not None else self.service
        pfx = prefix if prefix is not None else self.prefix
        routes = mount_rpc_routes(
            app=self.app,
            service=svc,
            target=target,
            prefix=pfx,
            verb_overrides=verb_overrides,
            path_overrides=path_overrides,
            description=description,
            service_name=service_name,
            tags=tags,
            **route_kwargs,
        )
        self._mounted_routes.extend(routes)
        target_id = (
            service_name
            or getattr(target, "service", None)
            or getattr(target, "name", None)
            or getattr(target, "__name__", str(target))
        )
        self._mounted_targets[str(target_id)] = target
        return routes


class AutoGatewayExtension(Extension):
    """Extension that dynamically mounts RPC routes on an HTTP gateway."""

    def __init__(
        self,
        targets: list[Any] | None = None,
        services: list[Any] | None = None,
        prefix: str = "",
        verb_overrides: dict[str, str] | None = None,
        path_overrides: dict[str, str] | None = None,
        http_extension: HttpExtension | None = None,
        host: str | None = None,
        port: int | None = None,
        **fastapi_kwargs: Any,
    ):
        resolved_targets = targets if targets is not None else services
        self._declared_targets = list(resolved_targets) if resolved_targets is not None else None
        self._prefix = prefix
        self._verb_overrides = MappingProxyType(dict(verb_overrides)) if verb_overrides else None
        self._path_overrides = MappingProxyType(dict(path_overrides)) if path_overrides else None
        self._http_extension = http_extension
        self._fastapi_kwargs = MappingProxyType(dict(fastapi_kwargs))
        self.host = host
        self.port = port

        # Per-instance state populated during setup()
        self.gateway: AutoGateway | None = None
        self.app: FastAPI | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    @property
    def mounted_routes(self) -> list[str]:
        if self.gateway is None:
            return []
        return self.gateway.mounted_routes

    def mount(
        self,
        target: Any,
        service: Any = None,
        prefix: str | None = None,
        verb_overrides: dict[str, str] | None = None,
        path_overrides: dict[str, str] | None = None,
        description: Description | None = None,
        service_name: str | None = None,
        tags: list[str | Enum] | None = None,
        **route_kwargs: Any,
    ) -> list[str]:
        if self.gateway is None:
            raise RuntimeError("AutoGatewayExtension.mount() called before setup()")
        return self.gateway.mount(
            target=target,
            service=service or self.service,
            prefix=prefix,
            verb_overrides=verb_overrides,
            path_overrides=path_overrides,
            description=description,
            service_name=service_name,
            tags=tags,
            **route_kwargs,
        )

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        service = ctx.service
        app: FastAPI | None = None

        if self._http_extension is not None:
            app = self._http_extension.app
        else:
            from .extension import HttpExtension

            for attr_name in dir(service):
                if attr_name.startswith("_"):
                    continue
                val = getattr(service, attr_name, None)
                if isinstance(val, HttpExtension):
                    self._http_extension = val
                    app = val.app
                    break

        if app is None:
            if self._http_extension is not None and self._http_extension.app is not None:
                app = self._http_extension.app
            else:
                fastapi_kwargs = {
                    "title": f"{ctx.service_config.name} Gateway",
                    **self._fastapi_kwargs,
                }
                app = FastAPI(**fastapi_kwargs)

        self.app = app
        self.gateway = AutoGateway(app=self.app, service=service, prefix=self._prefix)

        existing_paths = {getattr(route, "path", None) for route in self.app.routes}
        if "/live" not in existing_paths:

            @self.app.get("/live")
            @with_correlation_id
            async def live() -> JSONResponse:
                if hasattr(service, "liveness_check"):
                    data = service.liveness_check()
                    if asyncio.iscoroutine(data):
                        data = await data
                elif hasattr(service, "is_live"):
                    data = service.is_live()
                    if asyncio.iscoroutine(data):
                        data = await data
                else:
                    running = getattr(service, "_running", True)
                    service_name = getattr(getattr(service, "config", None), "name", "service")
                    data = {
                        "service": service_name,
                        "status": "healthy" if running else "stopped",
                    }
                return JSONResponse(
                    content=jsonable_encoder(data),
                    status_code=200 if data.get("status") == "healthy" else 503,
                )

        if "/ready" not in existing_paths:

            @self.app.get("/ready")
            @with_correlation_id
            async def ready() -> JSONResponse:
                body = await service.health_check()
                return JSONResponse(
                    content=jsonable_encoder(body),
                    status_code=200 if body.get("status") == "healthy" else 503,
                )

        if "/health" not in existing_paths:

            @self.app.get("/health")
            @with_correlation_id
            async def health() -> JSONResponse:
                body = await service.health_check()
                return JSONResponse(
                    content=jsonable_encoder(body),
                    status_code=200 if body.get("status") == "healthy" else 503,
                )

        if "/info" not in existing_paths:

            @self.app.get("/info")
            @with_correlation_id
            async def info() -> dict[str, Any]:
                return cast(dict[str, Any], service.get_service_info())

        if (
            self._http_extension is None
            and self.port is not None
            and hasattr(service, "health_listener")
        ):
            service.health_listener.disable(
                f"cliffracer_http gateway serves health endpoints on port {self.port}"
            )

        targets_to_mount = (
            self._declared_targets if self._declared_targets is not None else [service]
        )
        for target in targets_to_mount:
            try:
                self.gateway.mount(
                    target=target,
                    prefix=self._prefix,
                    verb_overrides=dict(self._verb_overrides) if self._verb_overrides else None,
                    path_overrides=dict(self._path_overrides) if self._path_overrides else None,
                )
            except ValueError as exc:
                logger.warning(f"Could not auto-mount target {target!r}: {exc}")

    async def start(self) -> None:
        if self._http_extension is None and self.port is not None and self.app is not None:
            host = self.host or "127.0.0.1"
            config = uvicorn.Config(app=self.app, host=host, port=self.port, log_level="info")
            self._server = uvicorn.Server(config)
            self._task = asyncio.create_task(self._server.serve())
            logger.info(f"AutoGateway server started on http://{host}:{self.port}")
        else:
            route_count = len(self.gateway.mounted_routes) if self.gateway else 0
            logger.info(f"AutoGatewayExtension started with {route_count} route(s)")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            if self._task is not None:
                try:
                    await asyncio.wait_for(self._task, timeout=5.0)
                except TimeoutError:
                    logger.warning("AutoGateway server shutdown timed out")
            self._server = None
        logger.info("AutoGatewayExtension stopped")

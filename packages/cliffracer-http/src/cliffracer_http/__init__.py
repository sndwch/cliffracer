"""HTTP routes and websockets for cliffracer services."""

from .config import HttpConfig
from .extension import HttpExtension
from .gateway import (
    AutoGateway,
    AutoGatewayExtension,
    generate_route_path,
    infer_http_verb,
    mount_rpc_routes,
)

__all__ = [
    "AutoGateway",
    "AutoGatewayExtension",
    "HttpConfig",
    "HttpExtension",
    "generate_route_path",
    "infer_http_verb",
    "mount_rpc_routes",
]

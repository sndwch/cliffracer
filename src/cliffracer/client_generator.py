"""
Simple client generator for Cliffracer services
"""

import json
from datetime import datetime
from typing import Any

import nats
from nats.errors import TimeoutError


class ClientGenerator:
    """Generate typed clients for Cliffracer services"""

    def __init__(self, nats_client: nats.NATS):
        self.nc = nats_client

    async def discover_service(self, service_name: str, timeout: float = 5.0) -> dict[str, Any] | None:
        """Discover service info by calling get_service_info"""
        try:
            subject = f"{service_name}.rpc.get_service_info"
            response = await self.nc.request(subject, b"{}", timeout=timeout)
            return json.loads(response.data.decode())
        except TimeoutError:
            return None
        except Exception:
            return None

    def generate_client_code(self, service_info: dict[str, Any]) -> str:
        """Generate Python client code from service info"""
        service_name = service_info["name"]
        service_version = service_info["version"]
        rpc_methods = service_info["rpc_methods"]

        # Filter out the introspection method
        user_methods = [m for m in rpc_methods if m != "get_service_info"]

        timestamp = datetime.now().isoformat()

        class_name = self._to_class_name(service_name)

        client_code = f'''"""
Generated client for {service_name} service
Service Version: {service_version}
Generated At: {timestamp}

This client provides typed access to {service_name} RPC methods.
"""

import json
from typing import Any

import nats
from nats.errors import TimeoutError


class {class_name}Client:
    """Generated client for {service_name} service"""

    SERVICE_NAME = "{service_name}"
    SERVICE_VERSION = "{service_version}"
    GENERATED_AT = "{timestamp}"

    def __init__(self, nats_client: nats.NATS, service_name: str = "{service_name}"):
        self.nc = nats_client
        self.service_name = service_name

    async def _call_rpc(self, method: str, **kwargs) -> Any:
        """Internal method to make RPC calls"""
        subject = f"{{self.service_name}}.rpc.{{method}}"
        payload = json.dumps(kwargs).encode()

        try:
            response = await self.nc.request(subject, payload, timeout=30.0)
            return json.loads(response.data.decode())
        except TimeoutError as e:
            raise TimeoutError(f"RPC call to {{method}} timed out") from e
        except Exception as e:
            raise RuntimeError(f"RPC call to {{method}} failed: {{e}}") from e
'''

        # Generate methods for each RPC endpoint
        for method in user_methods:
            method_code = f'''
    async def {method}(self, **kwargs) -> Any:
        """Call {method} RPC method"""
        return await self._call_rpc("{method}", **kwargs)
'''
            client_code += method_code

        # Add a convenience method to get service info
        client_code += '''
    async def get_service_info(self) -> dict[str, Any]:
        """Get service metadata and health info"""
        return await self._call_rpc("get_service_info")
'''

        return client_code

    def _to_class_name(self, service_name: str) -> str:
        """Convert service_name to ClassName"""
        # Convert snake_case or kebab-case to CamelCase
        parts = service_name.replace("-", "_").split("_")
        return "".join(word.capitalize() for word in parts)

    async def generate_client_file(self, service_name: str, output_path: str) -> bool:
        """Discover service and generate client file"""
        print(f"Discovering service: {service_name}")
        service_info = await self.discover_service(service_name)

        if not service_info:
            print(f"❌ Could not discover service: {service_name}")
            print("   Make sure the service is running and has get_service_info method")
            return False

        print(f"✅ Found service: {service_info['name']} v{service_info['version']}")
        print(f"   RPC methods: {', '.join(service_info['rpc_methods'])}")

        client_code = self.generate_client_code(service_info)

        with open(output_path, "w") as f:
            f.write(client_code)

        print(f"✅ Generated client: {output_path}")
        return True


# CLI interface for client generation
async def main():
    """Simple CLI for generating clients"""
    import sys

    if len(sys.argv) < 3:
        print("Usage: python -m cliffracer.client_generator <service_name> <output_file>")
        print("Example: python -m cliffracer.client_generator user_service user_service_client.py")
        sys.exit(1)

    service_name = sys.argv[1]
    output_file = sys.argv[2]

    # Connect to NATS
    try:
        nc = await nats.connect("nats://localhost:4222")
        generator = ClientGenerator(nc)

        success = await generator.generate_client_file(service_name, output_file)

        await nc.close()

        if success:
            print("\n🎉 Client generated successfully!")
            print(f"   Import with: from {output_file.replace('.py', '')} import {generator._to_class_name(service_name)}Client")
        else:
            sys.exit(1)

    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

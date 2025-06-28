#!/usr/bin/env python3
"""
Simple CLI tool for generating Cliffracer service clients

Usage:
    python scripts/generate_client.py user_service user_service_client.py
    python scripts/generate_client.py order_service clients/order_client.py
"""

import asyncio
import sys

# Add src to path so we can import cliffracer
sys.path.insert(0, "src")

import nats

from cliffracer.client_generator import ClientGenerator


async def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/generate_client.py <service_name> <output_file>")
        print()
        print("Examples:")
        print("  python scripts/generate_client.py user_service user_service_client.py")
        print("  python scripts/generate_client.py order_service clients/order_client.py")
        print()
        print("Make sure the target service is running and accessible via NATS.")
        sys.exit(1)

    service_name = sys.argv[1]
    output_file = sys.argv[2]

    print(f"🔍 Generating client for service: {service_name}")
    print(f"📁 Output file: {output_file}")
    print()

    try:
        # Connect to NATS (default localhost)
        print("🔌 Connecting to NATS (nats://localhost:4222)...")
        nc = await nats.connect("nats://localhost:4222")

        generator = ClientGenerator(nc)
        success = await generator.generate_client_file(service_name, output_file)

        await nc.close()

        if success:
            class_name = generator._to_class_name(service_name)
            module_name = output_file.replace('.py', '').replace('/', '.')

            print()
            print("🎉 Client generated successfully!")
            print()
            print("📖 Usage example:")
            print(f"   from {module_name} import {class_name}Client")
            print("   import nats")
            print()
            print("   nc = await nats.connect()")
            print(f"   client = {class_name}Client(nc)")
            print("   result = await client.your_method_name(param1='value')")
            print()
        else:
            print()
            print("❌ Failed to generate client")
            print("   Possible issues:")
            print("   - Service is not running")
            print("   - Service doesn't have get_service_info method")
            print("   - NATS connection failed")
            sys.exit(1)

    except Exception as e:
        print(f"❌ Error: {e}")
        print()
        print("🔧 Troubleshooting:")
        print("   - Make sure NATS server is running on localhost:4222")
        print("   - Make sure the target service is running")
        print("   - Check that the service name is correct")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

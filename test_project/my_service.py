#!/usr/bin/env python3
"""
Example business service using Cliffracer framework
"""

import asyncio

from pydantic import Field

from cliffracer import CliffracerService, event, rpc
from cliffracer.core.mixins import HTTPMixin
from cliffracer.database import SecureRepository
from cliffracer.database.models import DatabaseModel


# Define your business models
class Product(DatabaseModel):
    __tablename__ = "products"

    name: str = Field(..., description="Product name")
    price: float = Field(..., description="Product price")
    category: str = Field(..., description="Product category")
    in_stock: bool = Field(default=True, description="Is product in stock")


class MyBusinessService(CliffracerService, HTTPMixin):
    """Example business service with HTTP endpoints and database"""

    def __init__(self):
        from cliffracer import ServiceConfig
        config = ServiceConfig(
            name="my_business_service",
            nats_url="nats://localhost:4222"
        )
        super().__init__(config)
        self._http_port = 8080

        # Set up database repository
        self.products = SecureRepository(Product)

    @rpc
    async def create_product(self, name: str, price: float, category: str) -> dict:
        """Create a new product"""
        product = Product(name=name, price=price, category=category)
        created = await self.products.create(product)

        # Publish event for other services
        await self.publish_event("product.created", created.model_dump())

        return created.model_dump()

    @rpc
    async def get_product(self, product_id: str) -> dict:
        """Get a product by ID"""
        product = await self.products.get(product_id)
        return product.model_dump() if product else {"error": "Product not found"}

    @rpc
    async def list_products(self, category: str = None) -> dict:
        """List all products, optionally filtered by category"""
        if category:
            products = await self.products.find_by(category=category)
        else:
            products = await self.products.list(limit=50)

        return {
            "products": [p.model_dump() for p in products],
            "count": len(products)
        }

    @event("order.created")
    async def on_order_created(self, order_data: dict):
        """Handle when an order is created - update inventory"""
        product_id = order_data.get("product_id")
        if product_id:
            product = await self.products.get(product_id)
            if product and product.in_stock:
                print(f"Processing order for product: {product.name}")
                # Update inventory, send notifications, etc.


# Health check service
class HealthService(CliffracerService):
    """Simple health monitoring service"""

    def __init__(self):
        from cliffracer import ServiceConfig
        config = ServiceConfig(
            name="health_service",
            nats_url="nats://localhost:4222"
        )
        super().__init__(config)

    @rpc
    async def ping(self) -> str:
        """Simple ping endpoint"""
        return "pong"

    @rpc
    async def health_check(self) -> dict:
        """Comprehensive health check"""
        return {
            "status": "healthy",
            "service": self.name,
            "uptime": "unknown",  # You could track this
            "dependencies": {
                "nats": "connected",
                "database": "connected"
            }
        }


async def main():
    """Run multiple services using the orchestrator"""
    from cliffracer import ServiceOrchestrator

    orchestrator = ServiceOrchestrator()

    # Add our services
    orchestrator.add_service(MyBusinessService())
    orchestrator.add_service(HealthService())

    print("🚀 Starting business application with Cliffracer...")
    print("📡 NATS services running on nats://localhost:4222")
    print("🌐 HTTP API available at http://localhost:8080")
    print("📖 API docs at http://localhost:8080/docs")
    print("❤️  Health check: http://localhost:8080/health")
    print()
    print("Try these API calls:")
    print("  POST /products - Create a product")
    print("  GET /products - List all products")
    print("  GET /products/{id} - Get specific product")
    print()
    print("Press Ctrl+C to stop...")

    # Run the orchestrator
    await orchestrator.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Shutting down gracefully...")

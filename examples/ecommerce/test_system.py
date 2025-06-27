#!/usr/bin/env python3
"""
Live System Testing Script
=========================

This script demonstrates the running e-commerce system by making API calls
and showing the results in real-time. Run this while the main system is running
to see the services interact.
"""

import asyncio
import aiohttp
import json
import random
import time
from decimal import Decimal


async def test_order_creation():
    """Test order creation via HTTP API"""
    print("🛒 Testing Order Creation API")
    print("-" * 40)
    
    # Sample products
    products = [
        {"product_id": "laptop-pro", "name": "Professional Laptop", "price": "1299.99"},
        {"product_id": "smartphone-x", "name": "Smartphone X", "price": "899.99"},
        {"product_id": "tablet-air", "name": "Tablet Air", "price": "599.99"},
        {"product_id": "headphones-premium", "name": "Premium Headphones", "price": "199.99"},
        {"product_id": "mouse-wireless", "name": "Wireless Mouse", "price": "49.99"}
    ]
    
    async with aiohttp.ClientSession() as session:
        for i in range(3):
            # Create random order
            num_items = random.randint(1, 3)
            items = []
            
            for _ in range(num_items):
                product = random.choice(products)
                quantity = random.randint(1, 2)
                items.append({
                    "product_id": product["product_id"],
                    "name": product["name"],
                    "quantity": quantity,
                    "price": product["price"]
                })
            
            order_data = {
                "user_id": f"test_user_{random.randint(1, 100)}",
                "items": items,
                "shipping_address": f"{random.randint(100, 9999)} Test St, Test City",
                "email": f"test{random.randint(1, 100)}@example.com"
            }
            
            try:
                # Send POST request to create order
                start_time = time.time()
                async with session.post(
                    "http://localhost:8001/orders",
                    json=order_data,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    response_time = time.time() - start_time
                    
                    if response.status == 200:
                        order = await response.json()
                        print(f"✅ Order #{i+1} created successfully!")
                        print(f"   Order ID: {order['order_id']}")
                        print(f"   Total: ${order['total_amount']}")
                        print(f"   Items: {len(order['items'])}")
                        print(f"   Response time: {response_time:.3f}s")
                        print()
                    else:
                        error_text = await response.text()
                        print(f"❌ Order #{i+1} failed: {response.status} - {error_text}")
                        print()
                        
            except Exception as e:
                print(f"❌ Order #{i+1} error: {e}")
                print()
            
            # Wait between orders
            await asyncio.sleep(2)


async def test_order_retrieval():
    """Test order retrieval API"""
    print("📋 Testing Order Retrieval API")
    print("-" * 40)
    
    async with aiohttp.ClientSession() as session:
        try:
            # Get all orders
            async with session.get("http://localhost:8001/orders") as response:
                if response.status == 200:
                    data = await response.json()
                    orders = data.get("orders", [])
                    print(f"📊 Found {len(orders)} total orders in system")
                    
                    if orders:
                        # Show details of the latest order
                        latest_order = orders[-1]
                        print(f"\n📄 Latest Order Details:")
                        print(f"   Order ID: {latest_order['order_id']}")
                        print(f"   Status: {latest_order['status']}")
                        print(f"   User: {latest_order['user_id']}")
                        print(f"   Total: ${latest_order['total_amount']}")
                        print(f"   Created: {latest_order['created_at']}")
                        
                        # Test individual order retrieval
                        order_id = latest_order['order_id']
                        async with session.get(f"http://localhost:8001/orders/{order_id}") as detail_response:
                            if detail_response.status == 200:
                                order_detail = await detail_response.json()
                                print(f"✅ Individual order retrieval successful")
                                print(f"   Items: {len(order_detail['items'])}")
                            else:
                                print(f"❌ Individual order retrieval failed: {detail_response.status}")
                    else:
                        print("   No orders found - system might be starting up")
                        
                else:
                    print(f"❌ Failed to retrieve orders: {response.status}")
                    
        except Exception as e:
            print(f"❌ Order retrieval error: {e}")
        
        print()


async def test_api_documentation():
    """Test that API documentation is accessible"""
    print("📚 Testing API Documentation")
    print("-" * 40)
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("http://localhost:8001/docs") as response:
                if response.status == 200:
                    print("✅ API documentation is accessible at http://localhost:8001/docs")
                    print("   You can test the API interactively using the Swagger UI")
                else:
                    print(f"❌ API documentation not accessible: {response.status}")
                    
        except Exception as e:
            print(f"❌ API documentation error: {e}")
        
        print()


async def monitor_system_metrics():
    """Monitor basic system metrics"""
    print("📊 System Monitoring Information")
    print("-" * 40)
    
    print("🔍 Monitoring Endpoints Available:")
    print("   📊 NATS Monitoring: http://localhost:8222")
    print("   📈 Zabbix Dashboard: http://localhost:8080 (admin/zabbix)")
    print("   📋 Order API Docs: http://localhost:8001/docs")
    print("   🔧 Grafana (optional): http://localhost:3000 (admin/admin)")
    print()
    
    print("📝 Log Monitoring:")
    print("   - Watch the main terminal for structured JSON logs")
    print("   - Look for 'action' fields to track business events")
    print("   - Monitor 'processing_time' for performance metrics")
    print()
    
    print("🎯 Key Metrics to Watch in Zabbix:")
    print("   - orders.created (order creation rate)")
    print("   - payments.processed (payment success/failure rate)")
    print("   - inventory.reserved (inventory operations)")
    print("   - notifications.sent (notification delivery)")
    print("   - Service response times and error rates")
    print()


async def main():
    """Run all tests"""
    print("🚀 Cliffracer Live System Testing")
    print("=" * 50)
    print()
    
    print("This script will test the running e-commerce system.")
    print("Make sure the main system is running with: ./setup_live_demo.sh")
    print()
    
    # Check if the system is running
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("http://localhost:8001/docs") as response:
                if response.status != 200:
                    print("❌ Order service is not running at http://localhost:8001")
                    print("   Please start the system with: ./setup_live_demo.sh")
                    return
        except:
            print("❌ Cannot connect to order service at http://localhost:8001")
            print("   Please start the system with: ./setup_live_demo.sh")
            return
    
    print("✅ System appears to be running, starting tests...\n")
    
    # Run tests
    await test_order_creation()
    await asyncio.sleep(2)
    
    await test_order_retrieval()
    await asyncio.sleep(2)
    
    await test_api_documentation()
    await asyncio.sleep(2)
    
    await monitor_system_metrics()
    
    print("🎉 Testing Complete!")
    print()
    print("💡 Tips for monitoring:")
    print("   1. Keep this terminal open alongside the main system")
    print("   2. Open Zabbix at http://localhost:8080 to see real-time metrics")
    print("   3. Watch the main system logs for structured events")
    print("   4. Try the interactive API at http://localhost:8001/docs")


if __name__ == "__main__":
    asyncio.run(main())
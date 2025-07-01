"""
Travel Booking Saga Example

A simpler saga example demonstrating flight, hotel, and car rental booking
with automatic compensation on failure.
"""

import asyncio
import random
from datetime import datetime, timedelta
from typing import Dict, Any

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.patterns.saga import SagaCoordinator, SagaParticipant, SagaStep


class FlightService(CliffracerService, SagaParticipant):
    """Flight booking service"""
    
    def __init__(self):
        config = ServiceConfig(name="flight_service")
        CliffracerService.__init__(self, config)
        SagaParticipant.__init__(self, self)
        
        # In-memory bookings for demo
        self.bookings = {}
    
    def _register_handlers(self):
        """Register saga handlers"""
        
        @self.rpc
        async def book_flight(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Book a flight"""
            try:
                # Simulate booking logic
                booking_id = f"FL-{saga_id[:8]}"
                
                # Randomly fail 20% of the time for demo
                if random.random() < 0.2:
                    return {"error": "No flights available for selected dates"}
                
                self.bookings[booking_id] = {
                    "id": booking_id,
                    "passenger": data["passenger_name"],
                    "from": data["from_city"],
                    "to": data["to_city"],
                    "date": data["travel_date"],
                    "price": 350.00,
                    "status": "confirmed"
                }
                
                print(f"✈️  Flight booked: {booking_id}")
                
                return {
                    "result": {
                        "booking_id": booking_id,
                        "price": 350.00,
                        "flight_number": "AA123"
                    }
                }
            except Exception as e:
                return {"error": str(e)}
        
        @self.rpc
        async def cancel_flight(saga_id: str, correlation_id: str, step: str, data: dict, original_result: dict) -> dict:
            """Cancel flight booking (compensation)"""
            try:
                booking_id = original_result["booking_id"]
                if booking_id in self.bookings:
                    self.bookings[booking_id]["status"] = "cancelled"
                    print(f"❌ Flight cancelled: {booking_id}")
                
                return {"result": {"status": "cancelled"}}
            except Exception as e:
                return {"error": str(e)}


class HotelService(CliffracerService, SagaParticipant):
    """Hotel booking service"""
    
    def __init__(self):
        config = ServiceConfig(name="hotel_service")
        CliffracerService.__init__(self, config)
        SagaParticipant.__init__(self, self)
        
        self.bookings = {}
    
    def _register_handlers(self):
        """Register saga handlers"""
        
        @self.rpc
        async def book_hotel(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Book a hotel"""
            try:
                booking_id = f"HT-{saga_id[:8]}"
                
                # Simulate processing delay
                await asyncio.sleep(0.5)
                
                # Randomly fail 15% of the time
                if random.random() < 0.15:
                    return {"error": "No rooms available"}
                
                self.bookings[booking_id] = {
                    "id": booking_id,
                    "guest": data["passenger_name"],
                    "hotel": "Grand Plaza Hotel",
                    "check_in": data["travel_date"],
                    "check_out": data.get("return_date", data["travel_date"]),
                    "price": 120.00,
                    "status": "confirmed"
                }
                
                print(f"🏨 Hotel booked: {booking_id}")
                
                return {
                    "result": {
                        "booking_id": booking_id,
                        "price": 120.00,
                        "room_number": "405"
                    }
                }
            except Exception as e:
                return {"error": str(e)}
        
        @self.rpc
        async def cancel_hotel(saga_id: str, correlation_id: str, step: str, data: dict, original_result: dict) -> dict:
            """Cancel hotel booking (compensation)"""
            try:
                booking_id = original_result["booking_id"]
                if booking_id in self.bookings:
                    self.bookings[booking_id]["status"] = "cancelled"
                    print(f"❌ Hotel cancelled: {booking_id}")
                
                return {"result": {"status": "cancelled"}}
            except Exception as e:
                return {"error": str(e)}


class CarRentalService(CliffracerService, SagaParticipant):
    """Car rental service"""
    
    def __init__(self):
        config = ServiceConfig(name="car_rental_service")
        CliffracerService.__init__(self, config)
        SagaParticipant.__init__(self, self)
        
        self.bookings = {}
    
    def _register_handlers(self):
        """Register saga handlers"""
        
        @self.rpc
        async def book_car(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Book a rental car"""
            try:
                booking_id = f"CR-{saga_id[:8]}"
                
                # Randomly fail 10% of the time
                if random.random() < 0.1:
                    return {"error": "No cars available"}
                
                self.bookings[booking_id] = {
                    "id": booking_id,
                    "driver": data["passenger_name"],
                    "car_type": data.get("car_type", "Economy"),
                    "pickup_date": data["travel_date"],
                    "return_date": data.get("return_date", data["travel_date"]),
                    "price": 45.00,
                    "status": "confirmed"
                }
                
                print(f"🚗 Car booked: {booking_id}")
                
                return {
                    "result": {
                        "booking_id": booking_id,
                        "price": 45.00,
                        "car_model": "Toyota Corolla"
                    }
                }
            except Exception as e:
                return {"error": str(e)}
        
        @self.rpc
        async def cancel_car(saga_id: str, correlation_id: str, step: str, data: dict, original_result: dict) -> dict:
            """Cancel car rental (compensation)"""
            try:
                booking_id = original_result["booking_id"]
                if booking_id in self.bookings:
                    self.bookings[booking_id]["status"] = "cancelled"
                    print(f"❌ Car rental cancelled: {booking_id}")
                
                return {"result": {"status": "cancelled"}}
            except Exception as e:
                return {"error": str(e)}


class TravelBookingService(CliffracerService):
    """Travel booking orchestrator"""
    
    def __init__(self):
        config = ServiceConfig(name="travel_booking_service")
        super().__init__(config)
        
        self.coordinator = SagaCoordinator(self)
        
        # Define the travel booking saga
        self.coordinator.define_saga("travel_booking", [
            SagaStep(
                name="book_flight",
                service="flight_service",
                action="book_flight",
                compensation="cancel_flight",
                timeout=10.0,
                retry_count=2
            ),
            SagaStep(
                name="book_hotel",
                service="hotel_service",
                action="book_hotel",
                compensation="cancel_hotel",
                timeout=10.0,
                retry_count=2
            ),
            SagaStep(
                name="book_car",
                service="car_rental_service",
                action="book_car",
                compensation="cancel_car",
                timeout=10.0,
                retry_count=1  # Less critical, fewer retries
            )
        ])
    
    @property
    def rpc(self):
        """RPC decorator"""
        return self._rpc_decorator
    
    def _rpc_decorator(self, func):
        """Register RPC handler"""
        self._rpc_handlers[func.__name__] = func
        return func
    
    @rpc
    async def book_travel(
        self,
        passenger_name: str,
        from_city: str,
        to_city: str,
        travel_date: str,
        return_date: str = None,
        car_type: str = "Economy"
    ) -> dict:
        """Book complete travel package"""
        print(f"\n🌍 Starting travel booking for {passenger_name}")
        print(f"   Route: {from_city} → {to_city}")
        print(f"   Dates: {travel_date} - {return_date or 'One way'}")
        print("-" * 50)
        
        result = await self.coordinator._start_saga("travel_booking", {
            "passenger_name": passenger_name,
            "from_city": from_city,
            "to_city": to_city,
            "travel_date": travel_date,
            "return_date": return_date,
            "car_type": car_type
        })
        
        return result


async def demonstrate_saga():
    """Demonstrate the travel booking saga"""
    # Start all services
    services = [
        FlightService(),
        HotelService(),
        CarRentalService(),
        TravelBookingService()
    ]
    
    # Run services
    tasks = []
    for service in services:
        task = asyncio.create_task(service.run())
        tasks.append(task)
    
    # Wait for services to start
    await asyncio.sleep(2)
    
    # Get the booking service
    booking_service = services[-1]
    
    # Example 1: Successful booking
    print("\n" + "="*60)
    print("EXAMPLE 1: Attempting travel booking (may succeed or fail)")
    print("="*60)
    
    result1 = await booking_service.book_travel(
        passenger_name="John Doe",
        from_city="New York",
        to_city="San Francisco",
        travel_date="2024-03-15",
        return_date="2024-03-20",
        car_type="SUV"
    )
    
    print(f"\nBooking result: {result1}")
    
    # Wait and check status
    await asyncio.sleep(5)
    
    if "saga_id" in result1:
        status = await booking_service.rpc_call(
            "travel_booking_service.get_saga_status",
            {"saga_id": result1["saga_id"]}
        )
        print(f"\nFinal saga status: {status.get('state', 'Unknown')}")
        
        if status.get("state") == "COMPENSATED":
            print("❌ Booking failed and was compensated")
            print("   All successful bookings were automatically cancelled")
        elif status.get("state") == "COMPLETED":
            print("✅ Booking completed successfully!")
            print("   All services were booked")
    
    # Example 2: Another booking attempt
    print("\n" + "="*60)
    print("EXAMPLE 2: Another travel booking attempt")
    print("="*60)
    
    result2 = await booking_service.book_travel(
        passenger_name="Jane Smith",
        from_city="Los Angeles",
        to_city="Chicago",
        travel_date="2024-04-10",
        return_date="2024-04-15",
        car_type="Economy"
    )
    
    await asyncio.sleep(5)
    
    if "saga_id" in result2:
        status = await booking_service.rpc_call(
            "travel_booking_service.get_saga_status",
            {"saga_id": result2["saga_id"]}
        )
        print(f"\nFinal saga status: {status.get('state', 'Unknown')}")
    
    # Keep services running
    print("\n\nPress Ctrl+C to stop...")
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\nShutting down services...")
        for service in services:
            service.stop()


if __name__ == "__main__":
    print("🚀 Travel Booking Saga Demo")
    print("This demo shows automatic compensation when any step fails")
    print("Services randomly fail to demonstrate the saga pattern\n")
    
    asyncio.run(demonstrate_saga())
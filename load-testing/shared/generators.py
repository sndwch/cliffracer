"""
Data generators for load testing.

Provides realistic test data generation with configurable complexity
and error injection for comprehensive performance testing.
"""

import random
import secrets
from datetime import datetime, UTC, timedelta
from decimal import Decimal
from typing import Dict, List, Any, Optional
from uuid import uuid4

from faker import Faker
from .models import (
    Address, CustomerProfile, Product, ProductVariant, ProductCategory,
    OrderItem, ComplexOrder, PaymentDetails, PaymentMethod, ShippingDetails,
    AnalyticsEvent, BatchProcessingRequest, ValidationErrorTest, OrderStatus
)

fake = Faker()


class DataGenerator:
    """Generates realistic test data for load testing."""
    
    def __init__(self, error_rate: float = 0.05, locale: str = "en_US"):
        """
        Initialize data generator.
        
        Args:
            error_rate: Percentage of data that should trigger validation errors (0.0-1.0)
            locale: Faker locale for generating realistic data
        """
        self.error_rate = error_rate
        self.fake = Faker(locale)
        
        # Cache for consistent data across related objects
        self._product_cache: Dict[str, Product] = {}
        self._customer_cache: Dict[str, CustomerProfile] = {}
    
    def generate_address(self, residential: bool = True) -> Address:
        """Generate a realistic address."""
        return Address(
            street_address=self.fake.street_address(),
            apartment=self.fake.secondary_address() if random.random() < 0.3 else None,
            city=self.fake.city(),
            state=self.fake.state_abbr(),
            postal_code=self.fake.zipcode(),
            country="US",
            latitude=float(self.fake.latitude()) if random.random() < 0.5 else None,
            longitude=float(self.fake.longitude()) if random.random() < 0.5 else None,
            is_residential=residential,
            delivery_instructions=self.fake.text(max_nb_chars=200) if random.random() < 0.2 else None
        )
    
    def generate_customer_profile(self, customer_id: Optional[str] = None) -> CustomerProfile:
        """Generate a complex customer profile."""
        if customer_id and customer_id in self._customer_cache:
            return self._customer_cache[customer_id]
        
        customer_id = customer_id or f"CUST-{uuid4().hex[:8].upper()}"
        
        # Generate 1-3 addresses
        addresses = [self.generate_address() for _ in range(random.randint(1, 3))]
        
        # Add some complexity to preferences
        preferences = {
            "newsletter": random.choice([True, False]),
            "sms_notifications": random.choice([True, False]),
            "preferred_delivery_time": random.choice(["morning", "afternoon", "evening", "any"]),
            "special_instructions": self.fake.text(max_nb_chars=100) if random.random() < 0.3 else None,
            "communication_frequency": random.choice(["daily", "weekly", "monthly", "never"]),
            "interests": random.sample([
                "electronics", "fashion", "home", "books", "sports", "automotive", 
                "beauty", "toys", "garden", "health"
            ], k=random.randint(0, 5))
        }
        
        customer = CustomerProfile(
            customer_id=customer_id,
            email=self.fake.email(),
            phone=self.fake.phone_number(),
            first_name=self.fake.first_name(),
            last_name=self.fake.last_name(),
            date_of_birth=self.fake.date_of_birth(minimum_age=18, maximum_age=80),
            addresses=addresses,
            preferred_language=random.choice(["en", "es", "fr", "de"]),
            marketing_consent=random.choice([True, False]),
            loyalty_tier=random.choice(["bronze", "silver", "gold", "platinum"]),
            account_balance=Decimal(str(round(random.uniform(0, 1000), 2))),
            preferences=preferences
        )
        
        self._customer_cache[customer_id] = customer
        return customer
    
    def generate_product_variant(self, base_name: str) -> ProductVariant:
        """Generate a product variant with realistic attributes."""
        # Generate realistic attributes based on category
        attributes = {}
        if random.random() < 0.8:  # Most products have color
            attributes["color"] = random.choice([
                "red", "blue", "green", "black", "white", "gray", "navy", "brown", "pink", "purple"
            ])
        if random.random() < 0.6:  # Many products have size
            attributes["size"] = random.choice(["XS", "S", "M", "L", "XL", "XXL"])
        if random.random() < 0.4:  # Some have material
            attributes["material"] = random.choice([
                "cotton", "polyester", "leather", "metal", "plastic", "wood", "glass"
            ])
        
        # Ensure at least one attribute
        if not attributes:
            attributes["style"] = random.choice(["classic", "modern", "vintage", "premium"])
        
        return ProductVariant(
            sku=f"SKU-{secrets.token_hex(4).upper()}",
            name=f"{base_name} - {', '.join(f'{k}: {v}' for k, v in attributes.items())}",
            attributes=attributes,
            price=Decimal(str(round(random.uniform(9.99, 999.99), 2))),
            weight_grams=random.randint(50, 5000),
            dimensions_cm={
                "length": round(random.uniform(1.0, 50.0), 1),
                "width": round(random.uniform(1.0, 30.0), 1),
                "height": round(random.uniform(0.5, 20.0), 1)
            },
            stock_quantity=random.randint(0, 1000),
            is_digital=random.random() < 0.1  # 10% chance of digital product
        )
    
    def generate_product(self, product_id: Optional[str] = None) -> Product:
        """Generate a complex product with multiple variants."""
        if product_id and product_id in self._product_cache:
            return self._product_cache[product_id]
        
        product_id = product_id or f"PROD-{uuid4().hex[:8].upper()}"
        
        # Generate product name and description
        product_name = self.fake.catch_phrase()
        description = " ".join([
            self.fake.text(max_nb_chars=500),
            f"This {product_name.lower()} features premium quality and excellent value.",
            self.fake.text(max_nb_chars=300)
        ])
        
        # Generate 1-5 variants
        variants = [
            self.generate_product_variant(product_name) 
            for _ in range(random.randint(1, 5))
        ]
        
        # Generate tags
        tags = random.sample([
            "bestseller", "new-arrival", "sale", "premium", "eco-friendly", 
            "limited-edition", "trending", "featured", "recommended", "popular"
        ], k=random.randint(0, 4))
        
        # Generate image URLs
        images = [
            f"https://example.com/images/{product_id}/{i}.jpg" 
            for i in range(random.randint(1, 6))
        ]
        
        # Complex metadata
        metadata = {
            "manufacturer": self.fake.company(),
            "model_number": f"MODEL-{secrets.token_hex(3).upper()}",
            "warranty_months": random.choice([6, 12, 24, 36]),
            "country_of_origin": self.fake.country_code(),
            "compliance_certifications": random.sample([
                "CE", "FCC", "UL", "ISO9001", "RoHS", "ENERGY_STAR"
            ], k=random.randint(0, 3)),
            "technical_specs": {
                "power_consumption": f"{random.randint(5, 500)}W" if random.random() < 0.5 else None,
                "operating_temperature": f"{random.randint(-10, 50)}°C to {random.randint(60, 85)}°C" if random.random() < 0.3 else None,
                "connectivity": random.sample(["WiFi", "Bluetooth", "USB", "Ethernet"], k=random.randint(0, 2))
            }
        }
        
        product = Product(
            product_id=product_id,
            name=product_name,
            description=description,
            category=random.choice(list(ProductCategory)),
            brand=self.fake.company(),
            variants=variants,
            tags=tags,
            images=images,
            reviews_average=round(random.uniform(3.0, 5.0), 1),
            reviews_count=random.randint(0, 1000),
            metadata=metadata
        )
        
        self._product_cache[product_id] = product
        return product
    
    def generate_order_item(self, product: Optional[Product] = None) -> OrderItem:
        """Generate an order item with realistic pricing."""
        if product is None:
            product = self.generate_product()
        
        variant = random.choice(product.variants)
        quantity = random.randint(1, 5)
        unit_price = variant.price
        
        # Apply random discount
        discount_amount = Decimal("0.00")
        if random.random() < 0.2:  # 20% chance of discount
            discount_amount = round(unit_price * Decimal(str(random.uniform(0.05, 0.25))), 2)
        
        # Calculate tax (8.5% rate)
        subtotal = unit_price * quantity - discount_amount
        tax_amount = round(subtotal * Decimal("0.085"), 2)
        total_price = subtotal + tax_amount
        
        return OrderItem(
            product_id=product.product_id,
            variant_sku=variant.sku,
            quantity=quantity,
            unit_price=unit_price,
            discount_amount=discount_amount,
            tax_amount=tax_amount,
            total_price=total_price,
            product_snapshot=product,
            special_instructions=self.fake.text(max_nb_chars=200) if random.random() < 0.1 else None
        )
    
    def generate_payment_details(self, amount: Decimal) -> PaymentDetails:
        """Generate realistic payment details."""
        method = random.choice(list(PaymentMethod))
        
        payment = PaymentDetails(
            method=method,
            amount=amount,
            currency="USD",
            transaction_reference=f"TXN-{secrets.token_hex(8).upper()}",
            processed_at=datetime.now(UTC) if random.random() < 0.9 else None
        )
        
        # Add credit card details if applicable
        if method in [PaymentMethod.CREDIT_CARD, PaymentMethod.DEBIT_CARD]:
            payment.card_last_four = f"{random.randint(1000, 9999)}"
            payment.card_brand = random.choice(["Visa", "Mastercard", "American Express", "Discover"])
            payment.cardholder_name = f"{self.fake.first_name()} {self.fake.last_name()}"
        
        # Add risk scoring
        payment.risk_score = round(random.uniform(0.0, 1.0), 3)
        if payment.risk_score > 0.8:
            payment.fraud_flags = random.sample([
                "high_velocity", "unusual_location", "new_device", "suspicious_pattern"
            ], k=random.randint(1, 2))
        
        return payment
    
    def generate_shipping_details(self, address: Address, total_weight: int) -> ShippingDetails:
        """Generate shipping details based on order requirements."""
        carriers = ["FedEx", "UPS", "USPS", "DHL", "Amazon Logistics"]
        service_levels = ["standard", "express", "overnight", "two-day"]
        
        service_level = random.choice(service_levels)
        carrier = random.choice(carriers)
        
        # Calculate shipping cost based on weight and service level
        base_cost = Decimal("5.99")
        weight_cost = Decimal(str(total_weight)) * Decimal("0.001")  # $0.001 per gram
        service_multiplier = {
            "standard": Decimal("1.0"),
            "express": Decimal("1.5"),
            "two-day": Decimal("2.0"),
            "overnight": Decimal("3.0")
        }[service_level]
        
        shipping_cost = round((base_cost + weight_cost) * service_multiplier, 2)
        
        estimated_delivery = datetime.now(UTC) + timedelta(days=random.randint(1, 14))
        
        return ShippingDetails(
            carrier=carrier,
            service_level=service_level,
            tracking_number=f"{carrier[:3].upper()}{random.randint(10000000, 99999999)}" if random.random() < 0.8 else None,
            estimated_delivery=estimated_delivery,
            shipping_address=address,
            cost=shipping_cost,
            weight_grams=total_weight,
            package_dimensions={
                "length": round(random.uniform(10.0, 60.0), 1),
                "width": round(random.uniform(8.0, 40.0), 1),
                "height": round(random.uniform(5.0, 30.0), 1)
            },
            insurance_amount=Decimal("0.00") if random.random() < 0.7 else Decimal(str(random.randint(50, 500))),
            signature_required=random.random() < 0.3
        )
    
    def generate_complex_order(self, 
                             customer: Optional[CustomerProfile] = None,
                             num_items: Optional[int] = None,
                             force_error: bool = False) -> ComplexOrder:
        """
        Generate a complex order with realistic data and optional error injection.
        
        Args:
            customer: Use specific customer, or generate random one
            num_items: Number of items in order, or random 1-8
            force_error: Force validation errors for error testing
        """
        if customer is None:
            customer = self.generate_customer_profile()
        
        # Generate order items
        num_items = num_items or random.randint(1, 8)
        items = [self.generate_order_item() for _ in range(num_items)]
        
        # Calculate totals
        subtotal = sum(item.total_price for item in items)
        tax_amount = round(subtotal * Decimal("0.085"), 2)  # 8.5% tax
        
        # Generate shipping
        total_weight = sum(
            item.product_snapshot.variants[0].weight_grams * item.quantity 
            for item in items
        )
        shipping_address = random.choice(customer.addresses)
        shipping = self.generate_shipping_details(shipping_address, total_weight)
        shipping_cost = shipping.cost
        
        # Apply discounts
        discount_amount = Decimal("0.00")
        if random.random() < 0.3:  # 30% chance of order-level discount
            discount_amount = round(subtotal * Decimal(str(random.uniform(0.05, 0.2))), 2)
        
        total_amount = subtotal + tax_amount + shipping_cost - discount_amount
        
        # Generate payment
        payment = self.generate_payment_details(total_amount)
        
        # Error injection for testing
        if force_error or (random.random() < self.error_rate):
            # Introduce various types of validation errors
            error_type = random.choice([
                "negative_total",
                "invalid_email", 
                "future_birth_date",
                "invalid_totals",
                "missing_required"
            ])
            
            if error_type == "negative_total":
                total_amount = Decimal("-100.00")  # Invalid negative total
            elif error_type == "invalid_email":
                customer.email = "invalid-email"  # Invalid email format
            elif error_type == "future_birth_date":
                customer.date_of_birth = datetime.now(UTC) + timedelta(days=365)  # Future birth date
            elif error_type == "invalid_totals":
                subtotal = Decimal("1.00")  # Make totals inconsistent
        
        # Generate complex metadata
        metadata = {
            "browser_info": {
                "user_agent": self.fake.user_agent(),
                "ip_address": self.fake.ipv4(),
                "screen_resolution": f"{random.choice([1920, 1366, 1440, 1536])}x{random.choice([1080, 768, 900, 864])}",
                "timezone": random.choice(["EST", "PST", "CST", "MST"])
            },
            "marketing_data": {
                "utm_source": random.choice(["google", "facebook", "email", "direct"]),
                "utm_campaign": f"campaign_{random.randint(1, 100)}",
                "referrer": self.fake.url() if random.random() < 0.5 else None,
                "landing_page": f"/products/{random.choice(['electronics', 'clothing', 'home'])}"
            },
            "session_info": {
                "session_id": str(uuid4()),
                "page_views": random.randint(1, 20),
                "time_on_site_minutes": random.randint(2, 120),
                "previous_orders": random.randint(0, 50)
            }
        }
        
        return ComplexOrder(
            customer=customer,
            items=items,
            status=random.choice(list(OrderStatus)),
            subtotal=subtotal,
            tax_amount=tax_amount,
            shipping_cost=shipping_cost,
            discount_amount=discount_amount,
            total_amount=total_amount,
            payment=payment,
            shipping=shipping,
            expected_ship_date=datetime.now(UTC) + timedelta(days=random.randint(1, 5)),
            source=random.choice(["web", "mobile", "api", "phone"]),
            sales_channel=random.choice(["online", "retail", "wholesale", "marketplace"]),
            promotional_codes=[f"PROMO{random.randint(10, 99)}" for _ in range(random.randint(0, 2))],
            notes=self.fake.text(max_nb_chars=300) if random.random() < 0.2 else None,
            priority_level=random.randint(1, 5),
            requires_approval=total_amount > Decimal("5000.00"),
            fraud_check_passed=payment.risk_score < 0.8,
            metadata=metadata
        )
    
    def generate_analytics_event(self) -> AnalyticsEvent:
        """Generate a high-frequency analytics event."""
        event_types = [
            "page_view", "click", "scroll", "form_submit", "purchase", 
            "add_to_cart", "remove_from_cart", "search", "login", "logout"
        ]
        
        properties = {
            "page_url": self.fake.url(),
            "element_id": f"btn_{random.randint(1, 100)}",
            "value": random.uniform(0, 1000),
            "category": random.choice(["user_action", "system_event", "error", "conversion"]),
            "tags": random.sample(["mobile", "desktop", "tablet", "ios", "android", "chrome", "firefox"], k=random.randint(0, 3))
        }
        
        context = {
            "user_agent": self.fake.user_agent(),
            "ip": self.fake.ipv4(),
            "country": self.fake.country_code(),
            "city": self.fake.city(),
            "device_type": random.choice(["mobile", "desktop", "tablet"]),
            "screen_size": f"{random.choice([1920, 1366, 1440])}x{random.choice([1080, 768, 900])}"
        }
        
        return AnalyticsEvent(
            event_type=random.choice(event_types),
            user_id=f"user_{random.randint(1000, 9999)}" if random.random() < 0.8 else None,
            session_id=str(uuid4()),
            properties=properties,
            context=context,
            source=random.choice(["web", "mobile_app", "api", "webhook"])
        )
    
    def generate_batch_request(self, batch_size: int = 1000) -> BatchProcessingRequest:
        """Generate a large batch processing request."""
        operation_types = ["data_import", "image_processing", "report_generation", "data_export", "cleanup"]
        
        # Generate realistic batch items
        items = []
        for i in range(batch_size):
            items.append({
                "id": f"item_{i}",
                "data": {
                    "name": self.fake.name(),
                    "email": self.fake.email(),
                    "value": random.uniform(0, 1000),
                    "tags": random.sample(["tag1", "tag2", "tag3", "tag4", "tag5"], k=random.randint(0, 3)),
                    "nested_data": {
                        "level1": {
                            "level2": {
                                "level3": [random.randint(1, 100) for _ in range(random.randint(1, 10))]
                            }
                        }
                    }
                },
                "processing_options": {
                    "validate": True,
                    "transform": random.choice([True, False]),
                    "notify": random.choice([True, False])
                }
            })
        
        return BatchProcessingRequest(
            operation_type=random.choice(operation_types),
            items=items,
            options={
                "parallel_processing": True,
                "max_errors": 100,
                "retry_failed": True,
                "output_format": random.choice(["json", "csv", "xml"]),
                "compression": random.choice(["gzip", "bzip2", None])
            },
            priority=random.randint(1, 10),
            timeout_seconds=random.randint(300, 3600),
            callback_url=self.fake.url() if random.random() < 0.3 else None
        )
    
    def generate_validation_error_data(self) -> ValidationErrorTest:
        """Generate data that will trigger validation errors."""
        # Intentionally create invalid data
        return ValidationErrorTest(
            required_field="short",  # Too short - should be min 10 chars
            email_field="not-an-email",  # Invalid email format
            numeric_field=50,  # Out of range - should be 100-1000
            complex_pattern="INVALID-PATTERN"  # Doesn't match regex
        )


# Convenience functions for common use cases

def generate_test_orders(count: int, error_rate: float = 0.05) -> List[ComplexOrder]:
    """Generate multiple test orders with specified error rate."""
    generator = DataGenerator(error_rate=error_rate)
    return [generator.generate_complex_order() for _ in range(count)]


def generate_analytics_batch(count: int) -> List[AnalyticsEvent]:
    """Generate a batch of analytics events."""
    generator = DataGenerator()
    return [generator.generate_analytics_event() for _ in range(count)]


def generate_error_test_data(count: int) -> List[ValidationErrorTest]:
    """Generate data specifically designed to trigger validation errors."""
    generator = DataGenerator()
    return [generator.generate_validation_error_data() for _ in range(count)]
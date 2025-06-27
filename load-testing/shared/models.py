"""
Complex data models for load testing.

These models are designed to stress-test validation performance
with nested structures, complex validation rules, and large payloads.
"""

from datetime import datetime, UTC
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Dict, Any, Union
from uuid import uuid4

from pydantic import BaseModel, Field, validator, EmailStr


class OrderStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentMethod(str, Enum):
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    PAYPAL = "paypal"
    BANK_TRANSFER = "bank_transfer"
    CRYPTO = "crypto"


class ProductCategory(str, Enum):
    ELECTRONICS = "electronics"
    CLOTHING = "clothing"
    HOME_GARDEN = "home_garden"
    BOOKS = "books"
    SPORTS = "sports"
    AUTOMOTIVE = "automotive"


# Complex nested models for validation stress testing

class Address(BaseModel):
    """Address with comprehensive validation."""
    street_address: str = Field(..., min_length=5, max_length=200)
    apartment: Optional[str] = Field(None, max_length=50)
    city: str = Field(..., min_length=2, max_length=100)
    state: str = Field(..., min_length=2, max_length=50)
    postal_code: str = Field(..., pattern=r"^\d{5}(-\d{4})?$")
    country: str = Field(..., min_length=2, max_length=3)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    is_residential: bool = True
    delivery_instructions: Optional[str] = Field(None, max_length=500)


class CustomerProfile(BaseModel):
    """Complex customer profile with validation."""
    customer_id: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    phone: str = Field(..., pattern=r"^\+?1?\d{9,15}$")
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    date_of_birth: Optional[datetime] = None
    addresses: List[Address] = Field(..., min_items=1, max_items=10)
    preferred_language: str = Field(default="en", pattern=r"^[a-z]{2}$")
    marketing_consent: bool = False
    loyalty_tier: str = Field(default="bronze", pattern=r"^(bronze|silver|gold|platinum)$")
    account_balance: Decimal = Field(default=Decimal("0.00"), ge=0)
    preferences: Dict[str, Any] = Field(default_factory=dict)
    
    @validator('date_of_birth')
    def validate_age(cls, v):
        if v and v > datetime.now(UTC):
            raise ValueError("Date of birth cannot be in the future")
        return v


class ProductVariant(BaseModel):
    """Product variant with attributes."""
    sku: str = Field(..., min_length=3, max_length=50)
    name: str = Field(..., min_length=1, max_length=200)
    attributes: Dict[str, str] = Field(..., min_items=1)  # e.g., {"color": "red", "size": "L"}
    price: Decimal = Field(..., gt=0, max_digits=10, decimal_places=2)
    weight_grams: int = Field(..., gt=0, le=50000)  # Max 50kg
    dimensions_cm: Dict[str, float] = Field(...)  # {"length": 10.5, "width": 5.0, "height": 2.0}
    stock_quantity: int = Field(..., ge=0)
    is_digital: bool = False
    
    @validator('dimensions_cm')
    def validate_dimensions(cls, v):
        required_keys = {'length', 'width', 'height'}
        if not required_keys.issubset(v.keys()):
            raise ValueError(f"Missing required dimensions: {required_keys - v.keys()}")
        if any(dim <= 0 for dim in v.values()):
            raise ValueError("All dimensions must be positive")
        return v


class Product(BaseModel):
    """Complex product with multiple variants."""
    product_id: str = Field(..., min_length=3, max_length=50)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=10, max_length=2000)
    category: ProductCategory
    brand: str = Field(..., min_length=1, max_length=100)
    variants: List[ProductVariant] = Field(..., min_items=1, max_items=50)
    tags: List[str] = Field(default_factory=list, max_items=20)
    images: List[str] = Field(default_factory=list, max_items=10)  # URLs
    reviews_average: float = Field(default=0.0, ge=0.0, le=5.0)
    reviews_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: Dict[str, Any] = Field(default_factory=dict)


class OrderItem(BaseModel):
    """Order item with complex validation."""
    item_id: str = Field(default_factory=lambda: str(uuid4()))
    product_id: str = Field(..., min_length=3, max_length=50)
    variant_sku: str = Field(..., min_length=3, max_length=50)
    quantity: int = Field(..., gt=0, le=100)
    unit_price: Decimal = Field(..., gt=0, max_digits=10, decimal_places=2)
    discount_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    tax_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    total_price: Decimal = Field(..., gt=0, max_digits=10, decimal_places=2)
    product_snapshot: Product  # Full product data at time of order
    special_instructions: Optional[str] = Field(None, max_length=500)
    
    @validator('total_price')
    def validate_total_price(cls, v, values):
        if 'unit_price' in values and 'quantity' in values and 'discount_amount' in values:
            expected = (values['unit_price'] * values['quantity']) - values['discount_amount']
            if abs(v - expected) > Decimal("0.01"):  # Allow small rounding differences
                raise ValueError(f"Total price {v} doesn't match calculation {expected}")
        return v


class PaymentDetails(BaseModel):
    """Payment information with validation."""
    payment_id: str = Field(default_factory=lambda: str(uuid4()))
    method: PaymentMethod
    amount: Decimal = Field(..., gt=0, max_digits=10, decimal_places=2)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    transaction_reference: Optional[str] = Field(None, max_length=100)
    processed_at: Optional[datetime] = None
    gateway_response: Dict[str, Any] = Field(default_factory=dict)
    
    # Credit card specific (when applicable)
    card_last_four: Optional[str] = Field(None, pattern=r"^\d{4}$")
    card_brand: Optional[str] = Field(None, max_length=50)
    cardholder_name: Optional[str] = Field(None, max_length=100)
    
    # Additional payment metadata
    risk_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    fraud_flags: List[str] = Field(default_factory=list)


class ShippingDetails(BaseModel):
    """Shipping information with tracking."""
    shipping_id: str = Field(default_factory=lambda: str(uuid4()))
    carrier: str = Field(..., min_length=1, max_length=100)
    service_level: str = Field(..., min_length=1, max_length=100)  # "standard", "express", "overnight"
    tracking_number: Optional[str] = Field(None, max_length=100)
    estimated_delivery: Optional[datetime] = None
    actual_delivery: Optional[datetime] = None
    shipping_address: Address
    cost: Decimal = Field(..., ge=0, max_digits=10, decimal_places=2)
    weight_grams: int = Field(..., gt=0)
    package_dimensions: Dict[str, float] = Field(...)
    insurance_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    signature_required: bool = False
    
    @validator('actual_delivery')
    def validate_delivery_dates(cls, v, values):
        if v and 'estimated_delivery' in values and values['estimated_delivery']:
            if v < values['estimated_delivery'].replace(hour=0, minute=0, second=0, microsecond=0):
                # Allow early delivery, but not before the estimated date's day
                pass
        return v


class ComplexOrder(BaseModel):
    """
    Extremely complex order model for stress testing validation.
    This model includes nested validation, custom validators, and complex business logic.
    """
    order_id: str = Field(default_factory=lambda: f"ORD-{uuid4().hex[:8].upper()}")
    customer: CustomerProfile
    items: List[OrderItem] = Field(..., min_items=1, max_items=100)
    status: OrderStatus = OrderStatus.PENDING
    
    # Financial details
    subtotal: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    tax_amount: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=10, decimal_places=2)
    shipping_cost: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=10, decimal_places=2)
    discount_amount: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=10, decimal_places=2)
    total_amount: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    
    # Payment and shipping
    payment: PaymentDetails
    shipping: ShippingDetails
    
    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expected_ship_date: Optional[datetime] = None
    
    # Order metadata
    source: str = Field(default="web", max_length=50)  # "web", "mobile", "api", "phone"
    sales_channel: str = Field(default="online", max_length=50)
    promotional_codes: List[str] = Field(default_factory=list, max_items=5)
    notes: Optional[str] = Field(None, max_length=1000)
    internal_notes: Optional[str] = Field(None, max_length=1000)
    
    # Complex business logic validation
    priority_level: int = Field(default=1, ge=1, le=5)
    requires_approval: bool = False
    fraud_check_passed: bool = True
    
    # Large metadata field for stress testing
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    @validator('total_amount')
    def validate_total_amount(cls, v, values):
        """Complex validation of order total."""
        if all(key in values for key in ['subtotal', 'tax_amount', 'shipping_cost', 'discount_amount']):
            expected = values['subtotal'] + values['tax_amount'] + values['shipping_cost'] - values['discount_amount']
            if abs(v - expected) > Decimal("0.01"):
                raise ValueError(f"Total amount {v} doesn't match calculation {expected}")
        return v
    
    @validator('items')
    def validate_items_subtotal(cls, v, values):
        """Validate that items sum matches subtotal."""
        if v and 'subtotal' in values:
            items_total = sum(item.total_price for item in v)
            if abs(items_total - values['subtotal']) > Decimal("0.01"):
                raise ValueError(f"Items total {items_total} doesn't match subtotal {values['subtotal']}")
        return v
    
    @validator('expected_ship_date')
    def validate_ship_date(cls, v, values):
        """Validate shipping date is in the future."""
        if v and v <= datetime.now(UTC):
            raise ValueError("Expected ship date must be in the future")
        return v
    
    @validator('requires_approval')
    def check_approval_requirements(cls, v, values):
        """Auto-set approval requirement for high-value orders."""
        if 'total_amount' in values and values['total_amount'] > Decimal("10000.00"):
            return True
        return v


# Analytics and event models for high-frequency testing

class AnalyticsEvent(BaseModel):
    """High-frequency analytics event for performance testing."""
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: str = Field(..., min_length=1, max_length=100)
    user_id: Optional[str] = Field(None, max_length=100)
    session_id: Optional[str] = Field(None, max_length=100)
    
    # Event data (can be large)
    properties: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    
    # Processing metadata
    source: str = Field(..., max_length=100)
    version: str = Field(default="1.0.0")
    batch_id: Optional[str] = None


class BatchProcessingRequest(BaseModel):
    """Large batch processing request for throughput testing."""
    batch_id: str = Field(default_factory=lambda: str(uuid4()))
    operation_type: str = Field(..., min_length=1, max_length=100)
    items: List[Dict[str, Any]] = Field(..., min_items=1, max_items=10000)
    options: Dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=5, ge=1, le=10)
    timeout_seconds: int = Field(default=300, gt=0, le=3600)
    callback_url: Optional[str] = None
    
    # Large metadata for memory testing
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Error simulation models

class ValidationErrorTest(BaseModel):
    """Model designed to trigger validation errors for error handling testing."""
    required_field: str = Field(..., min_length=10)  # Will fail with short strings
    email_field: EmailStr  # Will fail with invalid emails
    numeric_field: int = Field(..., ge=100, le=1000)  # Will fail outside range
    complex_pattern: str = Field(..., pattern=r"^TEST-\d{4}-[A-Z]{3}$")  # Will fail with wrong pattern
    
    @validator('required_field')
    def custom_validation_that_can_fail(cls, v):
        if "FAIL" in v.upper():
            raise ValueError("This field cannot contain 'FAIL'")
        return v
"""
Base database model for Cliffracer services.

Provides a base class for database models with common fields and functionality.
"""

from datetime import UTC, datetime
from typing import Any, ClassVar, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class DatabaseModel(BaseModel):
    """
    Base model for database entities.

    Provides common fields and methods for database models:
    - id: UUID primary key
    - created_at: Timestamp when record was created
    - updated_at: Timestamp when record was last updated

    Subclasses should define:
    - __tablename__: str - The database table name
    - Additional fields as needed
    """

    # Table name must be defined by subclasses
    __tablename__: ClassVar[str]

    # Common fields for all database models
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Config:
        # Allow UUID serialization
        json_encoders = {
            UUID: str,
            datetime: lambda v: v.isoformat(),
        }

    def dict_for_db(self) -> dict[str, Any]:
        """
        Convert model to dict suitable for database operations.

        Returns:
            Dictionary with database-compatible values
        """
        data = self.model_dump()
        # Ensure updated_at is current when saving
        data["updated_at"] = datetime.now(UTC)
        return data

    @classmethod
    def from_db_record(cls, record: dict[str, Any]) -> "DatabaseModel":
        """
        Create model instance from database record.

        Args:
            record: Database record as dictionary

        Returns:
            Model instance
        """
        return cls(**record)

    @classmethod
    def get_create_table_sql(cls) -> str:
        """
        Get SQL to create table for this model.

        This is a simplified version - in production you'd want
        to use a proper migration tool like Alembic.

        Returns:
            CREATE TABLE SQL statement
        """
        # Map Python types to PostgreSQL types
        type_mapping = {
            str: "TEXT",
            int: "INTEGER",
            float: "DOUBLE PRECISION",
            bool: "BOOLEAN",
            datetime: "TIMESTAMP WITH TIME ZONE",
            UUID: "UUID",
            dict: "JSONB",
            list: "JSONB",
        }

        columns = []

        # Always include base columns
        columns.append("id UUID PRIMARY KEY DEFAULT gen_random_uuid()")
        columns.append("created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP")
        columns.append("updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP")

        # Add columns from model fields
        for field_name, field_info in cls.__fields__.items():
            # Skip base fields we already added
            if field_name in ["id", "created_at", "updated_at"]:
                continue

            # Get field type (compatible with Pydantic v2)
            field_type = getattr(field_info, 'annotation', getattr(field_info, 'type_', str))

            # Handle Optional types
            is_nullable = False
            if hasattr(field_type, "__origin__") and field_type.__origin__ is Union:
                # This is Optional[T] which is Union[T, None]
                args = field_type.__args__
                if type(None) in args:
                    is_nullable = True
                    # Get the non-None type
                    field_type = next(arg for arg in args if arg is not type(None))

            # Map to SQL type
            sql_type = type_mapping.get(field_type, "TEXT")

            # Build column definition
            column_def = f"{field_name} {sql_type}"

            # Add NOT NULL if not nullable and no default
            if not is_nullable and field_info.default is None:
                column_def += " NOT NULL"

            columns.append(column_def)

        # Build CREATE TABLE statement
        create_sql = f"""CREATE TABLE IF NOT EXISTS {cls.__tablename__} (
    {',\n    '.join(columns)}
);

-- Create updated_at trigger
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_{cls.__tablename__}_updated_at BEFORE UPDATE
    ON {cls.__tablename__} FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
"""

        return create_sql


class User(DatabaseModel):
    """Example User model"""

    __tablename__ = "users"

    user_id: str = Field(..., description="Unique user identifier")
    email: str = Field(..., description="User email address")
    name: str = Field(..., description="User full name")
    status: str = Field(default="active", description="User status")


class Order(DatabaseModel):
    """Example Order model"""

    __tablename__ = "orders"

    order_id: str = Field(..., description="Unique order identifier")
    user_id: str = Field(..., description="User who placed the order")
    total_amount: float = Field(..., description="Total order amount")
    status: str = Field(default="pending", description="Order status")
    shipping_address: str = Field(..., description="Shipping address")
    email: str = Field(..., description="Customer email")


class Product(DatabaseModel):
    """Example Product model"""

    __tablename__ = "products"

    product_id: str = Field(..., description="Unique product identifier")
    name: str = Field(..., description="Product name")
    price: float = Field(..., description="Product price")
    quantity: int = Field(..., description="Available quantity")
    description: str | None = Field(None, description="Product description")

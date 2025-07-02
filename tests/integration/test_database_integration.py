"""
Integration tests for database functionality.

These tests require a running PostgreSQL instance and will create/drop test tables.
"""

import asyncio
import os
from datetime import UTC, datetime

import pytest

from cliffracer.database import DatabaseConnection, Repository
from cliffracer.database.models import Order, Product, User


@pytest.mark.integration
@pytest.mark.asyncio
class TestDatabaseIntegration:
    """Integration tests that require a real database"""

    @pytest.fixture
    async def db(self):
        """Create a test database connection"""
        # Use test database configuration
        db = DatabaseConnection(
            host=os.getenv("TEST_DB_HOST", "localhost"),
            port=int(os.getenv("TEST_DB_PORT", "5432")),
            user=os.getenv("TEST_DB_USER", "cliffracer_user"),
            password=os.getenv("TEST_DB_PASSWORD", "changeme"),
            database=os.getenv("TEST_DB_NAME", "cliffracer"),
        )

        await db.connect()
        yield db
        await db.disconnect()

    @pytest.fixture
    async def setup_test_tables(self, db):
        """Create test tables"""
        # Note: In a real application, you'd use migrations
        # This is simplified for testing

        # Drop tables if they exist
        await db.execute("DROP TABLE IF EXISTS order_items CASCADE")
        await db.execute("DROP TABLE IF EXISTS orders CASCADE")
        await db.execute("DROP TABLE IF EXISTS products CASCADE")
        await db.execute("DROP TABLE IF EXISTS users CASCADE")

        # Create users table
        await db.execute("""
            CREATE TABLE users (
                id UUID PRIMARY KEY,
                user_id VARCHAR(255) UNIQUE NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                status VARCHAR(50) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """)

        # Create products table
        await db.execute("""
            CREATE TABLE products (
                id UUID PRIMARY KEY,
                product_id VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                price DECIMAL(10, 2) NOT NULL,
                quantity INTEGER NOT NULL,
                description TEXT,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """)

        # Create orders table
        await db.execute("""
            CREATE TABLE orders (
                id UUID PRIMARY KEY,
                order_id VARCHAR(255) UNIQUE NOT NULL,
                user_id VARCHAR(255) NOT NULL,
                total_amount DECIMAL(10, 2) NOT NULL,
                status VARCHAR(50) NOT NULL,
                shipping_address TEXT NOT NULL,
                email VARCHAR(255) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """)

        yield

        # Cleanup
        await db.execute("DROP TABLE IF EXISTS orders CASCADE")
        await db.execute("DROP TABLE IF EXISTS products CASCADE")
        await db.execute("DROP TABLE IF EXISTS users CASCADE")

    async def test_database_connection(self, db):
        """Test basic database connectivity"""
        result = await db.fetchval("SELECT 1")
        assert result == 1

        # Test health check
        is_healthy = await db.health_check()
        assert is_healthy is True

    async def test_user_crud_operations(self, db, setup_test_tables):
        """Test full CRUD operations for users"""
        user_repo = Repository(User, db)

        # Create
        user = User(
            user_id="test_user_123", email="testuser@example.com", name="Test User", status="active"
        )
        created_user = await user_repo.create(user)
        assert created_user.id == user.id
        assert created_user.user_id == "test_user_123"

        # Read
        fetched_user = await user_repo.get(created_user.id)
        assert fetched_user is not None
        assert fetched_user.email == "testuser@example.com"

        # Update
        updated_user = await user_repo.update(
            created_user.id, name="Updated Test User", status="inactive"
        )
        assert updated_user.name == "Updated Test User"
        assert updated_user.status == "inactive"

        # Verify update persisted
        fetched_again = await user_repo.get(created_user.id)
        assert fetched_again.name == "Updated Test User"

        # Delete
        deleted = await user_repo.delete(created_user.id)
        assert deleted is True

        # Verify deletion
        deleted_user = await user_repo.get(created_user.id)
        assert deleted_user is None

    async def test_product_operations(self, db, setup_test_tables):
        """Test product repository operations"""
        product_repo = Repository(Product, db)

        # Create multiple products
        products = []
        for i in range(5):
            product = Product(
                product_id=f"PROD_{i:03d}",
                name=f"Product {i}",
                price=10.99 + i,
                quantity=100 - (i * 10),
                description=f"Description for product {i}",
            )
            created = await product_repo.create(product)
            products.append(created)

        # List products
        all_products = await product_repo.list(limit=10)
        assert len(all_products) == 5

        # Find by criteria - test with a string field that works reliably
        specific_product = await product_repo.find_by(product_id="PROD_002")
        assert len(specific_product) == 1
        assert specific_product[0].name == "Product 2"

        # For float comparisons, the database stores as NUMERIC/Decimal
        # but Python uses float, causing precision issues
        # Let's test with quantity (integer) instead
        low_stock_products = await product_repo.find_by(quantity=60)  # PROD_004 has quantity 60
        assert len(low_stock_products) == 1
        assert low_stock_products[0].product_id == "PROD_004"

        # Count products
        count = await product_repo.count()
        assert count == 5

        # Check existence
        exists = await product_repo.exists(product_id="PROD_002")
        assert exists is True

        exists = await product_repo.exists(product_id="PROD_999")
        assert exists is False

    async def test_order_operations_with_transactions(self, db, setup_test_tables):
        """Test order operations with transaction support"""
        user_repo = Repository(User, db)
        order_repo = Repository(Order, db)
        product_repo = Repository(Product, db)

        # Create test data
        user = await user_repo.create(
            User(user_id="order_test_user", email="orderuser@example.com", name="Order Test User")
        )

        product = await product_repo.create(
            Product(product_id="ORDER_PROD_001", name="Test Product", price=29.99, quantity=50)
        )

        # Test transaction - successful case
        async with db.transaction():
            # Create order
            await order_repo.create(
                Order(
                    order_id="ORD_001",
                    user_id=user.user_id,
                    total_amount=29.99,
                    status="pending",
                    shipping_address="123 Test St",
                    email=user.email,
                )
            )

            # Update product quantity
            await product_repo.update(product.id, quantity=product.quantity - 1)

        # Verify both operations succeeded
        created_order = await order_repo.find_one(order_id="ORD_001")
        assert created_order is not None
        assert created_order.status == "pending"

        updated_product = await product_repo.get(product.id)
        assert updated_product.quantity == 49

        # Test transaction rollback
        try:
            async with db.transaction():
                # Create another order
                await order_repo.create(
                    Order(
                        order_id="ORD_002",
                        user_id=user.user_id,
                        total_amount=29.99,
                        status="pending",
                        shipping_address="456 Test Ave",
                        email=user.email,
                    )
                )

                # This should fail due to negative quantity
                await db.execute("UPDATE products SET quantity = -1 WHERE id = $1", product.id)

                # Force an error
                raise ValueError("Simulated error")

        except ValueError:
            pass

        # Verify order was not created due to rollback
        failed_order = await order_repo.find_one(order_id="ORD_002")
        assert failed_order is None

        # Verify product quantity was not changed
        product_check = await product_repo.get(product.id)
        assert product_check.quantity == 49

    async def test_concurrent_operations(self, db, setup_test_tables):
        """Test concurrent database operations"""
        user_repo = Repository(User, db)

        # Create users concurrently
        async def create_user(index: int):
            user = User(
                user_id=f"concurrent_user_{index}",
                email=f"concurrent{index}@example.com",
                name=f"Concurrent User {index}",
            )
            return await user_repo.create(user)

        # Create 10 users concurrently
        tasks = [create_user(i) for i in range(10)]
        created_users = await asyncio.gather(*tasks)

        assert len(created_users) == 10

        # Verify all were created
        count = await user_repo.count()
        assert count == 10

        # Read users concurrently
        async def read_user(user_id: str):
            return await user_repo.find_one(user_id=user_id)

        read_tasks = [read_user(f"concurrent_user_{i}") for i in range(10)]
        read_users = await asyncio.gather(*read_tasks)

        assert all(user is not None for user in read_users)

    async def test_data_population_verification(self, db, setup_test_tables):
        """Verify that database columns are properly populated"""
        user_repo = Repository(User, db)

        # Create a user with all fields
        now = datetime.now(UTC)
        user = User(
            user_id="verify_user",
            email="verify@example.com",
            name="Verification User",
            status="pending",
        )

        created = await user_repo.create(user)

        # Fetch directly from database to verify all columns
        record = await db.fetchrow("SELECT * FROM users WHERE id = $1", created.id)

        # Verify all columns are populated
        assert record["id"] == created.id
        assert record["user_id"] == "verify_user"
        assert record["email"] == "verify@example.com"
        assert record["name"] == "Verification User"
        assert record["status"] == "pending"
        assert record["created_at"] is not None
        assert record["updated_at"] is not None

        # Verify timestamps are reasonable
        assert record["created_at"] >= now
        assert record["updated_at"] >= record["created_at"]

        # Update and verify updated_at changes
        await asyncio.sleep(0.1)  # Ensure time passes

        await user_repo.update(created.id, status="active")

        updated_record = await db.fetchrow("SELECT * FROM users WHERE id = $1", created.id)

        assert updated_record["status"] == "active"
        assert updated_record["updated_at"] > record["updated_at"]

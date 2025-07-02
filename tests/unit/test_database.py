"""
Unit tests for database functionality.

Tests database connection, models, and repository operations.
"""

import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from cliffracer.database import DatabaseConnection, Repository, get_db_connection
from cliffracer.database.models import User


class TestDatabaseConnection:
    """Test database connection functionality"""

    @pytest.fixture
    def db_config(self):
        """Database configuration for tests"""
        return {
            "host": "localhost",
            "port": 5432,
            "user": "test_user",
            "password": "test_pass",
            "database": "test_db",
        }

    def test_dsn_building(self, db_config):
        """Test DSN construction from parameters"""
        db = DatabaseConnection(**db_config)
        expected_dsn = "postgresql://test_user:test_pass@localhost:5432/test_db"
        assert db.dsn == expected_dsn

    def test_dsn_from_environment(self):
        """Test DSN construction from environment variables"""
        with patch.dict(os.environ, {
            "DB_HOST": "env_host",
            "DB_PORT": "5433",
            "DB_USER": "env_user",
            "DB_PASSWORD": "env_pass",
            "DB_NAME": "env_db",
        }):
            db = DatabaseConnection()
            expected_dsn = "postgresql://env_user:env_pass@env_host:5433/env_db"
            assert db.dsn == expected_dsn

    @pytest.mark.asyncio
    async def test_connect_creates_pool(self, db_config):
        """Test that connect creates a connection pool"""
        db = DatabaseConnection(**db_config)

        mock_pool = AsyncMock()
        with patch("cliffracer.database.connection.asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool) as mock_create:
            await db.connect()

            # Verify pool was created with correct parameters
            mock_create.assert_called_once_with(
                db.dsn,
                min_size=10,
                max_size=20,
                command_timeout=60,
            )
            assert db.pool == mock_pool

    @pytest.mark.asyncio
    async def test_disconnect_closes_pool(self, db_config):
        """Test that disconnect closes the pool"""
        db = DatabaseConnection(**db_config)

        mock_pool = AsyncMock()
        db.pool = mock_pool

        await db.disconnect()

        mock_pool.close.assert_called_once()
        assert db.pool is None

    @pytest.mark.asyncio
    async def test_execute_auto_connects(self, db_config):
        """Test that execute auto-connects if pool is None"""
        db = DatabaseConnection(**db_config)

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_acquire_ctx = AsyncMock()
        mock_acquire_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_acquire_ctx
        mock_conn.execute.return_value = "INSERT 0 1"

        with patch("cliffracer.database.connection.asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool):
            result = await db.execute("INSERT INTO test VALUES ($1)", "value")

            assert result == "INSERT 0 1"
            mock_conn.execute.assert_called_once_with(
                "INSERT INTO test VALUES ($1)", "value", timeout=None
            )

    @pytest.mark.asyncio
    async def test_fetch_operations(self, db_config):
        """Test fetch, fetchrow, and fetchval operations"""
        db = DatabaseConnection(**db_config)

        # Mock records
        mock_record1 = {"id": 1, "name": "test1"}
        mock_record2 = {"id": 2, "name": "test2"}

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_acquire_ctx = AsyncMock()
        mock_acquire_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_acquire_ctx

        db.pool = mock_pool

        # Test fetch (multiple records)
        mock_conn.fetch.return_value = [mock_record1, mock_record2]
        records = await db.fetch("SELECT * FROM test")
        assert len(records) == 2
        mock_conn.fetch.assert_called_once()

        # Test fetchrow (single record)
        mock_conn.fetchrow.return_value = mock_record1
        record = await db.fetchrow("SELECT * FROM test WHERE id = $1", 1)
        assert record == mock_record1

        # Test fetchval (single value)
        mock_conn.fetchval.return_value = 42
        value = await db.fetchval("SELECT COUNT(*) FROM test")
        assert value == 42

    @pytest.mark.asyncio
    async def test_transaction_context(self, db_config):
        """Test transaction context manager"""
        db = DatabaseConnection(**db_config)

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_transaction = AsyncMock()

        # Create a proper async context manager for pool.acquire()
        mock_acquire_ctx = AsyncMock()
        mock_acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_pool.acquire.return_value = mock_acquire_ctx

        # Create a proper async context manager for conn.transaction()
        mock_transaction_ctx = AsyncMock()
        mock_transaction_ctx.__aenter__ = AsyncMock(return_value=mock_transaction)
        mock_transaction_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=mock_transaction_ctx)

        db.pool = mock_pool

        # Execute queries within transaction
        async with db.transaction():
            # Pool should be temporarily replaced with connection
            assert db.pool == mock_conn

        # Pool should be restored after transaction
        assert db.pool == mock_pool

    @pytest.mark.asyncio
    async def test_health_check(self, db_config):
        """Test database health check"""
        db = DatabaseConnection(**db_config)

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_acquire_ctx = AsyncMock()
        mock_acquire_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_acquire_ctx

        # Test healthy connection
        with patch("cliffracer.database.connection.asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool):
            mock_conn.fetchval.return_value = 1
            db.pool = mock_pool

            is_healthy = await db.health_check()
            assert is_healthy is True
            mock_conn.fetchval.assert_called_with("SELECT 1", column=0, timeout=None)

        # Test unhealthy connection
        mock_conn.fetchval.side_effect = Exception("Connection failed")
        is_healthy = await db.health_check()
        assert is_healthy is False


class TestDatabaseModel:
    """Test database model functionality"""

    def test_model_defaults(self):
        """Test that model has default values"""
        user = User(user_id="user123", email="test@example.com", name="Test User")

        # Check defaults
        assert isinstance(user.id, UUID)
        assert isinstance(user.created_at, datetime)
        assert isinstance(user.updated_at, datetime)
        assert user.status == "active"

    def test_dict_for_db(self):
        """Test conversion to database dictionary"""
        original_time = datetime(2023, 1, 1, tzinfo=UTC)
        user = User(
            user_id="user123",
            email="test@example.com",
            name="Test User",
            created_at=original_time,
            updated_at=original_time,
        )

        db_dict = user.dict_for_db()

        # Check that all fields are present
        assert "id" in db_dict
        assert db_dict["user_id"] == "user123"
        assert db_dict["email"] == "test@example.com"

        # updated_at should be current time
        assert db_dict["updated_at"] > original_time

    def test_from_db_record(self):
        """Test creating model from database record"""
        record = {
            "id": uuid4(),
            "user_id": "user123",
            "email": "test@example.com",
            "name": "Test User",
            "status": "active",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

        user = User.from_db_record(record)

        assert user.id == record["id"]
        assert user.user_id == record["user_id"]
        assert user.email == record["email"]

    def test_model_json_serialization(self):
        """Test that models can be serialized to JSON"""
        user = User(user_id="user123", email="test@example.com", name="Test User")

        # Should not raise an error
        json_data = user.model_dump_json()
        assert "user123" in json_data
        assert "test@example.com" in json_data


class TestRepository:
    """Test repository operations"""

    @pytest.fixture
    def mock_db(self):
        """Mock database connection"""
        return AsyncMock(spec=DatabaseConnection)

    @pytest.fixture
    def user_repo(self, mock_db):
        """User repository with mocked database"""
        return Repository(User, mock_db)

    @pytest.mark.asyncio
    async def test_create(self, user_repo, mock_db):
        """Test creating a record"""
        user = User(user_id="user123", email="test@example.com", name="Test User")

        # Mock the database response
        mock_record = {
            "id": user.id,
            "user_id": user.user_id,
            "email": user.email,
            "name": user.name,
            "status": user.status,
            "created_at": user.created_at,
            "updated_at": datetime.now(UTC),
        }
        mock_db.fetchrow.return_value = mock_record

        created_user = await user_repo.create(user)

        # Verify the query was called correctly
        mock_db.fetchrow.assert_called_once()
        query = mock_db.fetchrow.call_args[0][0]
        assert "INSERT INTO users" in query
        assert "RETURNING *" in query

        # Verify the created user
        assert created_user.user_id == user.user_id
        assert created_user.email == user.email

    @pytest.mark.asyncio
    async def test_get_by_id(self, user_repo, mock_db):
        """Test getting a record by ID"""
        user_id = uuid4()

        # Mock found record
        mock_record = {
            "id": user_id,
            "user_id": "user123",
            "email": "test@example.com",
            "name": "Test User",
            "status": "active",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        mock_db.fetchrow.return_value = mock_record

        user = await user_repo.get(user_id)

        assert user is not None
        assert user.id == user_id
        assert user.user_id == "user123"

        # Test not found
        mock_db.fetchrow.return_value = None
        user = await user_repo.get(uuid4())
        assert user is None

    @pytest.mark.asyncio
    async def test_find_by_criteria(self, user_repo, mock_db):
        """Test finding records by criteria"""
        # Mock multiple records
        mock_records = [
            {
                "id": uuid4(),
                "user_id": f"user{i}",
                "email": f"user{i}@example.com",
                "name": f"User {i}",
                "status": "active",
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            for i in range(3)
        ]
        mock_db.fetch.return_value = mock_records

        # Find by status
        users = await user_repo.find_by(status="active")

        assert len(users) == 3
        mock_db.fetch.assert_called_once()
        query = mock_db.fetch.call_args[0][0]
        assert "WHERE status = $1" in query
        assert mock_db.fetch.call_args[0][1] == "active"

    @pytest.mark.asyncio
    async def test_update(self, user_repo, mock_db):
        """Test updating a record"""
        user_id = uuid4()

        # Mock updated record
        mock_record = {
            "id": user_id,
            "user_id": "user123",
            "email": "newemail@example.com",
            "name": "Updated User",
            "status": "active",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        mock_db.fetchrow.return_value = mock_record

        updated_user = await user_repo.update(
            user_id,
            email="newemail@example.com",
            name="Updated User"
        )

        assert updated_user is not None
        assert updated_user.email == "newemail@example.com"
        assert updated_user.name == "Updated User"

        # Verify query
        query = mock_db.fetchrow.call_args[0][0]
        assert "UPDATE users" in query
        assert "SET" in query
        assert "RETURNING *" in query

    @pytest.mark.asyncio
    async def test_delete(self, user_repo, mock_db):
        """Test deleting a record"""
        user_id = uuid4()

        # Mock successful deletion
        mock_db.fetchval.return_value = user_id

        deleted = await user_repo.delete(user_id)
        assert deleted is True

        # Mock not found
        mock_db.fetchval.return_value = None
        deleted = await user_repo.delete(uuid4())
        assert deleted is False

    @pytest.mark.asyncio
    async def test_list_with_pagination(self, user_repo, mock_db):
        """Test listing records with pagination"""
        mock_records = [
            {
                "id": uuid4(),
                "user_id": f"user{i}",
                "email": f"user{i}@example.com",
                "name": f"User {i}",
                "status": "active",
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            for i in range(5)
        ]
        mock_db.fetch.return_value = mock_records

        await user_repo.list(limit=10, offset=20)

        mock_db.fetch.assert_called_once()
        query = mock_db.fetch.call_args[0][0]
        assert "LIMIT $1 OFFSET $2" in query
        assert mock_db.fetch.call_args[0][1] == 10  # limit
        assert mock_db.fetch.call_args[0][2] == 20  # offset

    @pytest.mark.asyncio
    async def test_count(self, user_repo, mock_db):
        """Test counting records"""
        mock_db.fetchval.return_value = 42

        # Count all
        count = await user_repo.count()
        assert count == 42

        # Count with criteria
        count = await user_repo.count(status="active")
        assert count == 42

        query = mock_db.fetchval.call_args[0][0]
        assert "SELECT COUNT(*)" in query
        assert "WHERE status = $1" in query

    @pytest.mark.asyncio
    async def test_exists(self, user_repo, mock_db):
        """Test checking existence"""
        # Exists
        mock_db.fetchval.return_value = 1
        exists = await user_repo.exists(email="test@example.com")
        assert exists is True

        # Does not exist
        mock_db.fetchval.return_value = 0
        exists = await user_repo.exists(email="notfound@example.com")
        assert exists is False


class TestGlobalConnection:
    """Test global database connection singleton"""

    def test_get_db_connection_singleton(self):
        """Test that get_db_connection returns the same instance"""
        db1 = get_db_connection()
        db2 = get_db_connection()

        assert db1 is db2

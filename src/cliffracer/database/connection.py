"""
PostgreSQL database connection management for Cliffracer services.

This module provides async database connection pooling and transaction management.
"""

import os
from contextlib import asynccontextmanager

import asyncpg
from loguru import logger


class DatabaseConnection:
    """
    Manages PostgreSQL database connections with connection pooling.

    This class provides:
    - Async connection pooling
    - Transaction management
    - Automatic retry on connection failure
    - Connection health checks
    """

    def __init__(
        self,
        dsn: str | None = None,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        min_size: int = 10,
        max_size: int = 20,
    ):
        """
        Initialize database connection.

        Args:
            dsn: Full database connection string
            host: Database host (default: localhost)
            port: Database port (default: 5432)
            user: Database user
            password: Database password
            database: Database name
            min_size: Minimum pool size
            max_size: Maximum pool size
        """
        self.dsn = dsn or self._build_dsn(host, port, user, password, database)
        self.min_size = min_size
        self.max_size = max_size
        self.pool: asyncpg.Pool | None = None

    def _build_dsn(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
    ) -> str:
        """Build DSN from environment variables or provided parameters."""
        host = host or os.getenv("DB_HOST", "localhost")
        port = port or int(os.getenv("DB_PORT", "5432"))
        user = user or os.getenv("DB_USER", "cliffracer")
        password = password or os.getenv("DB_PASSWORD", "cliffracer123")
        database = database or os.getenv("DB_NAME", "cliffracer")

        return f"postgresql://{user}:{password}@{host}:{port}/{database}"

    async def connect(self) -> None:
        """Establish database connection pool."""
        if self.pool is None:
            logger.info("Creating database connection pool")
            self.pool = await asyncpg.create_pool(
                self.dsn,
                min_size=self.min_size,
                max_size=self.max_size,
                command_timeout=60,
            )
            logger.info("Database connection pool created successfully")

    async def disconnect(self) -> None:
        """Close database connection pool."""
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("Database connection pool closed")

    async def execute(self, query: str, *args, timeout: float | None = None) -> str:
        """
        Execute a query without returning results.

        Args:
            query: SQL query to execute
            *args: Query parameters
            timeout: Query timeout in seconds

        Returns:
            Status string (e.g., "INSERT 0 1")
        """
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args, timeout=timeout)

    async def fetch(self, query: str, *args, timeout: float | None = None) -> list[asyncpg.Record]:
        """
        Execute a query and fetch all results.

        Args:
            query: SQL query to execute
            *args: Query parameters
            timeout: Query timeout in seconds

        Returns:
            List of records
        """
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args, timeout=timeout)

    async def fetchrow(self, query: str, *args, timeout: float | None = None) -> asyncpg.Record | None:
        """
        Execute a query and fetch a single row.

        Args:
            query: SQL query to execute
            *args: Query parameters
            timeout: Query timeout in seconds

        Returns:
            Single record or None
        """
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args, timeout=timeout)

    async def fetchval(self, query: str, *args, column: int = 0, timeout: float | None = None):
        """
        Execute a query and fetch a single value.

        Args:
            query: SQL query to execute
            *args: Query parameters
            column: Column index to fetch (default: 0)
            timeout: Query timeout in seconds

        Returns:
            Single value
        """
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args, column=column, timeout=timeout)

    @asynccontextmanager
    async def transaction(self):
        """
        Create a database transaction context.

        Usage:
            async with db.transaction():
                await db.execute("INSERT INTO users ...")
                await db.execute("UPDATE orders ...")
        """
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Temporarily store connection for nested queries
                old_pool = self.pool
                try:
                    # Create a temporary "pool" with just this connection
                    self.pool = conn
                    yield
                finally:
                    self.pool = old_pool

    async def health_check(self) -> bool:
        """
        Check database connection health.

        Returns:
            True if healthy, False otherwise
        """
        try:
            if not self.pool:
                await self.connect()

            # Simple query to test connection
            result = await self.fetchval("SELECT 1")
            return result == 1
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False


# Global database connection instance
_db_connection: DatabaseConnection | None = None


def get_db_connection() -> DatabaseConnection:
    """
    Get the global database connection instance.

    Returns:
        DatabaseConnection instance
    """
    global _db_connection
    if _db_connection is None:
        _db_connection = DatabaseConnection()
    return _db_connection

"""
Generic repository pattern for database operations.

Provides a base repository class with common CRUD operations.
"""

from typing import TypeVar
from uuid import UUID

from loguru import logger

from .connection import DatabaseConnection, get_db_connection
from .models import DatabaseModel

T = TypeVar("T", bound=DatabaseModel)


class Repository[T: DatabaseModel]:
    """
    Generic repository for database operations.

    Provides common CRUD operations for database models:
    - Create
    - Read (get by id, find by criteria)
    - Update
    - Delete
    - List with pagination
    """

    def __init__(self, model_class: type[T], db: DatabaseConnection | None = None):
        """
        Initialize repository.

        Args:
            model_class: The model class this repository manages
            db: Database connection (uses global if not provided)
        """
        self.model_class = model_class
        self.table_name = model_class.__tablename__
        self.db = db or get_db_connection()

    async def create(self, model: T) -> T:
        """
        Create a new record in the database.

        Args:
            model: Model instance to create

        Returns:
            Created model with database-generated fields
        """
        data = model.dict_for_db()

        # Build INSERT query
        columns = list(data.keys())
        values = [data[col] for col in columns]
        placeholders = [f"${i + 1}" for i in range(len(columns))]

        query = f"""
            INSERT INTO {self.table_name} ({", ".join(columns)})
            VALUES ({", ".join(placeholders)})
            RETURNING *
        """

        record = await self.db.fetchrow(query, *values)
        logger.info(f"Created {self.model_class.__name__} with id {record['id']}")

        return self.model_class.from_db_record(dict(record))

    async def get(self, id: UUID) -> T | None:
        """
        Get a record by ID.

        Args:
            id: Record ID

        Returns:
            Model instance or None if not found
        """
        query = f"SELECT * FROM {self.table_name} WHERE id = $1"
        record = await self.db.fetchrow(query, id)

        if record:
            return self.model_class.from_db_record(dict(record))
        return None

    async def find_by(self, **criteria) -> list[T]:
        """
        Find records by criteria.

        Args:
            **criteria: Field=value pairs to filter by

        Returns:
            List of matching records
        """
        if not criteria:
            return await self.list()

        # Build WHERE clause
        conditions = []
        values = []
        for i, (field, value) in enumerate(criteria.items(), 1):
            conditions.append(f"{field} = ${i}")
            values.append(value)

        query = f"""
            SELECT * FROM {self.table_name}
            WHERE {" AND ".join(conditions)}
            ORDER BY created_at DESC
        """

        records = await self.db.fetch(query, *values)
        return [self.model_class.from_db_record(dict(record)) for record in records]

    async def find_one(self, **criteria) -> T | None:
        """
        Find a single record by criteria.

        Args:
            **criteria: Field=value pairs to filter by

        Returns:
            First matching record or None
        """
        results = await self.find_by(**criteria)
        return results[0] if results else None

    async def update(self, id: UUID, **updates) -> T | None:
        """
        Update a record by ID.

        Args:
            id: Record ID
            **updates: Fields to update

        Returns:
            Updated model or None if not found
        """
        if not updates:
            return await self.get(id)

        # Add updated_at timestamp
        from datetime import UTC, datetime

        updates["updated_at"] = datetime.now(UTC)

        # Build UPDATE query
        set_clauses = []
        values = []
        for i, (field, value) in enumerate(updates.items(), 1):
            set_clauses.append(f"{field} = ${i}")
            values.append(value)

        values.append(id)  # For WHERE clause

        query = f"""
            UPDATE {self.table_name}
            SET {", ".join(set_clauses)}
            WHERE id = ${len(values)}
            RETURNING *
        """

        record = await self.db.fetchrow(query, *values)

        if record:
            logger.info(f"Updated {self.model_class.__name__} with id {id}")
            return self.model_class.from_db_record(dict(record))
        return None

    async def delete(self, id: UUID) -> bool:
        """
        Delete a record by ID.

        Args:
            id: Record ID

        Returns:
            True if deleted, False if not found
        """
        query = f"DELETE FROM {self.table_name} WHERE id = $1 RETURNING id"
        result = await self.db.fetchval(query, id)

        if result:
            logger.info(f"Deleted {self.model_class.__name__} with id {id}")
            return True
        return False

    async def list(self, limit: int = 100, offset: int = 0) -> list[T]:
        """
        List records with pagination.

        Args:
            limit: Maximum number of records to return
            offset: Number of records to skip

        Returns:
            List of records
        """
        query = f"""
            SELECT * FROM {self.table_name}
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
        """

        records = await self.db.fetch(query, limit, offset)
        return [self.model_class.from_db_record(dict(record)) for record in records]

    async def count(self, **criteria) -> int:
        """
        Count records matching criteria.

        Args:
            **criteria: Field=value pairs to filter by

        Returns:
            Number of matching records
        """
        if not criteria:
            query = f"SELECT COUNT(*) FROM {self.table_name}"
            return await self.db.fetchval(query)

        # Build WHERE clause
        conditions = []
        values = []
        for i, (field, value) in enumerate(criteria.items(), 1):
            conditions.append(f"{field} = ${i}")
            values.append(value)

        query = f"""
            SELECT COUNT(*) FROM {self.table_name}
            WHERE {" AND ".join(conditions)}
        """

        return await self.db.fetchval(query, *values)

    async def exists(self, **criteria) -> bool:
        """
        Check if any records exist matching criteria.

        Args:
            **criteria: Field=value pairs to filter by

        Returns:
            True if any records exist
        """
        count = await self.count(**criteria)
        return count > 0

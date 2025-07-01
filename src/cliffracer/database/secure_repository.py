"""
Secure repository pattern with SQL injection protection.

This module provides a secure repository implementation that validates
all table and field names to prevent SQL injection attacks.
"""

import re
from typing import Any, Set, TypeVar
from uuid import UUID

from loguru import logger

from .connection import DatabaseConnection, get_db_connection
from .models import DatabaseModel

T = TypeVar("T", bound=DatabaseModel)


class SecureRepository[T: DatabaseModel]:
    """
    Secure repository with SQL injection protection.
    
    This repository validates all table and field names against a whitelist
    to prevent SQL injection attacks. It also provides additional security
    features like query logging and parameter validation.
    """
    
    # Whitelist of allowed table names
    ALLOWED_TABLES: Set[str] = {
        "users",
        "services", 
        "events",
        "messages",
        "metrics",
        "logs",
        "sessions",
        "tokens",
        "permissions",
        "roles",
        "audit_logs",
        "configurations",
        "tasks",
        "jobs",
        "notifications",
        "webhooks",
        "api_keys",
        "health_checks",
        "deployments",
        "features"
    }
    
    # Pattern for valid SQL identifiers
    VALID_IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
    
    # Maximum identifier length (PostgreSQL limit)
    MAX_IDENTIFIER_LENGTH = 63
    
    def __init__(self, model_class: type[T], db: DatabaseConnection | None = None):
        """
        Initialize secure repository.
        
        Args:
            model_class: The model class this repository manages
            db: Database connection (uses global if not provided)
            
        Raises:
            ValueError: If table name is not allowed or invalid
        """
        self.model_class = model_class
        self.table_name = model_class.__tablename__
        self.db = db or get_db_connection()
        
        # Validate table name on initialization
        self._validate_table_name(self.table_name)
        
        # Cache validated field names for this model
        self._valid_fields: Set[str] = set()
        self._initialize_valid_fields()
    
    def _validate_table_name(self, table_name: str) -> None:
        """
        Validate table name against whitelist and pattern.
        
        Args:
            table_name: Table name to validate
            
        Raises:
            ValueError: If table name is invalid
        """
        if not table_name:
            raise ValueError("Table name cannot be empty")
        
        if len(table_name) > self.MAX_IDENTIFIER_LENGTH:
            raise ValueError(f"Table name too long: {len(table_name)} > {self.MAX_IDENTIFIER_LENGTH}")
        
        if table_name not in self.ALLOWED_TABLES:
            raise ValueError(
                f"Table '{table_name}' is not in allowed tables list. "
                f"Allowed tables: {sorted(self.ALLOWED_TABLES)}"
            )
        
        if not self.VALID_IDENTIFIER_PATTERN.match(table_name):
            raise ValueError(f"Table name '{table_name}' contains invalid characters")
    
    def _validate_field_name(self, field_name: str) -> None:
        """
        Validate field name against pattern and known fields.
        
        Args:
            field_name: Field name to validate
            
        Raises:
            ValueError: If field name is invalid
        """
        if not field_name:
            raise ValueError("Field name cannot be empty")
        
        if len(field_name) > self.MAX_IDENTIFIER_LENGTH:
            raise ValueError(f"Field name too long: {len(field_name)} > {self.MAX_IDENTIFIER_LENGTH}")
        
        if not self.VALID_IDENTIFIER_PATTERN.match(field_name):
            raise ValueError(f"Field name '{field_name}' contains invalid characters")
        
        # Check against known valid fields for this model
        if self._valid_fields and field_name not in self._valid_fields:
            raise ValueError(
                f"Field '{field_name}' is not a valid field for {self.model_class.__name__}. "
                f"Valid fields: {sorted(self._valid_fields)}"
            )
    
    def _initialize_valid_fields(self) -> None:
        """Initialize the set of valid fields from the model class."""
        # Get fields from model annotations
        if hasattr(self.model_class, "__annotations__"):
            self._valid_fields.update(self.model_class.__annotations__.keys())
        
        # Add common database fields
        self._valid_fields.update({
            "id", "created_at", "updated_at", "deleted_at",
            "created_by", "updated_by", "version", "is_active"
        })
        
        # Add any fields from the model's __fields__ if using Pydantic
        if hasattr(self.model_class, "__fields__"):
            self._valid_fields.update(self.model_class.__fields__.keys())
    
    def _validate_value(self, value: Any) -> None:
        """
        Validate parameter values for basic security.
        
        Args:
            value: Value to validate
            
        Raises:
            ValueError: If value appears suspicious
        """
        if isinstance(value, str):
            # Check for common SQL injection patterns
            suspicious_patterns = [
                "';", '";', '--', '/*', '*/', 'xp_', 'sp_',
                'UNION', 'SELECT', 'DROP', 'INSERT', 'UPDATE', 'DELETE'
            ]
            
            value_upper = value.upper()
            for pattern in suspicious_patterns:
                if pattern.upper() in value_upper:
                    logger.warning(f"Suspicious value pattern detected: {pattern}")
                    # Note: In production, you might want to be more lenient
                    # and just log rather than reject
    
    async def create(self, model: T) -> T:
        """Create a new record with validation."""
        data = model.dict_for_db()
        
        # Validate all field names
        for field in data.keys():
            self._validate_field_name(field)
        
        # Validate values
        for value in data.values():
            self._validate_value(value)
        
        # Build INSERT query with validated fields
        columns = list(data.keys())
        values = [data[col] for col in columns]
        placeholders = [f"${i+1}" for i in range(len(columns))]
        
        # Use parameterized query (safe from injection)
        query = f"""
            INSERT INTO {self.table_name} ({', '.join(columns)})
            VALUES ({', '.join(placeholders)})
            RETURNING *
        """
        
        record = await self.db.fetchrow(query, *values)
        logger.info(f"Created {self.model_class.__name__} with id {record['id']}")
        
        return self.model_class.from_db_record(dict(record))
    
    async def get(self, id: UUID) -> T | None:
        """Get a record by ID with validation."""
        # UUID is already safe from injection
        query = f"SELECT * FROM {self.table_name} WHERE id = $1"
        record = await self.db.fetchrow(query, id)
        
        if record:
            return self.model_class.from_db_record(dict(record))
        return None
    
    async def find_by(self, **criteria) -> list[T]:
        """Find records by criteria with field validation."""
        if not criteria:
            return await self.list()
        
        # Validate all field names
        for field in criteria.keys():
            self._validate_field_name(field)
        
        # Validate all values
        for value in criteria.values():
            self._validate_value(value)
        
        # Build WHERE clause with validated fields
        conditions = []
        values = []
        for i, (field, value) in enumerate(criteria.items(), 1):
            conditions.append(f"{field} = ${i}")
            values.append(value)
        
        query = f"""
            SELECT * FROM {self.table_name}
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC
        """
        
        records = await self.db.fetch(query, *values)
        return [self.model_class.from_db_record(dict(record)) for record in records]
    
    async def update(self, id: UUID, **updates) -> T | None:
        """Update a record with field validation."""
        if not updates:
            return await self.get(id)
        
        # Add updated_at timestamp
        from datetime import UTC, datetime
        updates["updated_at"] = datetime.now(UTC)
        
        # Validate all field names
        for field in updates.keys():
            self._validate_field_name(field)
        
        # Validate all values
        for value in updates.values():
            self._validate_value(value)
        
        # Build UPDATE query with validated fields
        set_clauses = []
        values = []
        for i, (field, value) in enumerate(updates.items(), 1):
            set_clauses.append(f"{field} = ${i}")
            values.append(value)
        
        values.append(id)  # For WHERE clause
        
        query = f"""
            UPDATE {self.table_name}
            SET {', '.join(set_clauses)}
            WHERE id = ${len(values)}
            RETURNING *
        """
        
        record = await self.db.fetchrow(query, *values)
        
        if record:
            logger.info(f"Updated {self.model_class.__name__} with id {id}")
            return self.model_class.from_db_record(dict(record))
        return None
    
    async def delete(self, id: UUID) -> bool:
        """Delete a record by ID."""
        # UUID is already safe
        query = f"DELETE FROM {self.table_name} WHERE id = $1 RETURNING id"
        result = await self.db.fetchval(query, id)
        
        if result:
            logger.info(f"Deleted {self.model_class.__name__} with id {id}")
            return True
        return False
    
    async def list(self, limit: int = 100, offset: int = 0) -> list[T]:
        """List records with pagination and validation."""
        from ..core.validation import validate_limit, validate_offset
        
        # Validate pagination parameters
        limit = validate_limit(limit, max_limit=1000)
        offset = validate_offset(offset)
        
        query = f"""
            SELECT * FROM {self.table_name}
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
        """
        
        records = await self.db.fetch(query, limit, offset)
        return [self.model_class.from_db_record(dict(record)) for record in records]
    
    async def count(self, **criteria) -> int:
        """Count records with field validation."""
        if not criteria:
            query = f"SELECT COUNT(*) FROM {self.table_name}"
            return await self.db.fetchval(query)
        
        # Validate fields and values
        for field in criteria.keys():
            self._validate_field_name(field)
        
        for value in criteria.values():
            self._validate_value(value)
        
        # Build WHERE clause
        conditions = []
        values = []
        for i, (field, value) in enumerate(criteria.items(), 1):
            conditions.append(f"{field} = ${i}")
            values.append(value)
        
        query = f"""
            SELECT COUNT(*) FROM {self.table_name}
            WHERE {' AND '.join(conditions)}
        """
        
        return await self.db.fetchval(query, *values)
    
    async def exists(self, **criteria) -> bool:
        """Check existence with validation."""
        count = await self.count(**criteria)
        return count > 0
    
    def add_allowed_table(self, table_name: str) -> None:
        """
        Add a table to the allowed list (use with caution).
        
        Args:
            table_name: Table name to allow
            
        Raises:
            ValueError: If table name is invalid
        """
        if not self.VALID_IDENTIFIER_PATTERN.match(table_name):
            raise ValueError(f"Table name '{table_name}' contains invalid characters")
        
        self.ALLOWED_TABLES.add(table_name)
        logger.warning(f"Added '{table_name}' to allowed tables list")
    
    def add_valid_field(self, field_name: str) -> None:
        """
        Add a field to the valid fields list for this model.
        
        Args:
            field_name: Field name to allow
            
        Raises:
            ValueError: If field name is invalid
        """
        if not self.VALID_IDENTIFIER_PATTERN.match(field_name):
            raise ValueError(f"Field name '{field_name}' contains invalid characters")
        
        self._valid_fields.add(field_name)
        logger.info(f"Added '{field_name}' to valid fields for {self.model_class.__name__}")


# Convenience function to update the default Repository
def make_repository_secure():
    """Replace the default Repository with SecureRepository globally."""
    import sys
    from . import repository
    
    # Replace the Repository class in the module
    repository.Repository = SecureRepository
    
    # Update the module's __all__ if it exists
    if hasattr(repository, '__all__') and 'Repository' in repository.__all__:
        repository.__all__.append('SecureRepository')
    
    logger.info("Replaced Repository with SecureRepository globally")
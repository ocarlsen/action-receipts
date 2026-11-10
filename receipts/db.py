"""Database connection and schema setup."""
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "schema.sql"


def dsn() -> str:
    return os.environ.get("DATABASE_URL", "postgresql:///fernhollow")


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or dsn(), row_factory=dict_row)


def reset_schema(conn: psycopg.Connection) -> None:
    """Drop and recreate both schemas. Destroys all data."""
    conn.execute(SCHEMA_SQL.read_text())
    conn.commit()

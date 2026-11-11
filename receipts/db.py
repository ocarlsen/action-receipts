"""Database connection and schema setup."""
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "schema.sql"


def dsn() -> str:
    return os.environ.get("DATABASE_URL", "postgresql:///fernhollow")


def canary_dsn() -> str:
    """Where canary ticks are read from (the dashboard). The hourly canary job runs against its own
    database (it resets the fake core each tick), so point this at that database; unset, it is the
    default database."""
    return os.environ.get("CANARY_DATABASE_URL") or dsn()


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or dsn(), row_factory=dict_row)


def reset_schema(conn: psycopg.Connection) -> None:
    """Drop and recreate both schemas. Destroys all data."""
    conn.execute(SCHEMA_SQL.read_text())
    conn.commit()

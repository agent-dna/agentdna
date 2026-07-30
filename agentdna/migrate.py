"""
Run database migrations for CBAC pgvector tables.

Usage: python -m agentdna.migrate
"""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

_SQL_DIR = Path(__file__).parent / "sql"
_DEFAULT_DSN = "postgresql://localhost/agentdna"


def get_dsn() -> str:
    return os.environ.get("AGENTDNA_DATABASE_URL", "") or _DEFAULT_DSN


def init_db(dsn: str = "") -> None:
    """Run schema.sql against the target database."""
    dsn = dsn or get_dsn()
    schema = (_SQL_DIR / "schema.sql").read_text()

    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(schema)
    conn.close()

    print(f"schema applied to: {dsn}")


if __name__ == "__main__":
    init_db()

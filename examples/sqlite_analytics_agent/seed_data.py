from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from mcp_server import initialize_database


def seed_database(reset: bool = False) -> dict[str, int]:
    """Create a missing or schema-less database, then seed deterministic data without duplication."""
    database = settings.database_path
    if reset and database.exists():
        database.unlink()
    initialize_database()
    with sqlite3.connect(database) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("customers", "products", "orders", "order_items")
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the SQLite analytics database with deterministic sample data.")
    parser.add_argument("--reset", action="store_true", help="Delete the configured database before seeding it.")
    return parser.parse_args()


def main() -> None:
    print(seed_database(reset=parse_args().reset))


if __name__ == "__main__":
    main()
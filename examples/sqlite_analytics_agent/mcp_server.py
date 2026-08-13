from __future__ import annotations

import sqlite3
from pathlib import Path

from fastmcp import FastMCP

from config import settings

mcp = FastMCP("sqlite-analytics-mcp")
TABLES = {"customers", "products", "orders", "order_items"}


def _connect(read_only: bool = False) -> sqlite3.Connection:
    database = settings.database_path.resolve()
    if read_only:
        return sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    return sqlite3.connect(database)


def initialize_database() -> None:
    database: Path = settings.database_path
    database.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL, FOREIGN KEY(customer_id) REFERENCES customers(id));
            CREATE TABLE IF NOT EXISTS order_items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity INTEGER NOT NULL, unit_price REAL NOT NULL, FOREIGN KEY(order_id) REFERENCES orders(id), FOREIGN KEY(product_id) REFERENCES products(id));
            """
        )
        if connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
            connection.executemany("INSERT INTO customers VALUES (?, ?, ?)", [(1, "Avery Lee", "2026-01-10"), (2, "Morgan Patel", "2026-02-08"), (3, "Sam Rivera", "2026-03-02")])
            connection.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", [(1, "Secure Gateway", "security", 199.0), (2, "Data Console", "analytics", 99.0), (3, "Cloud Monitor", "infrastructure", 149.0)])
            connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", [(1, 1, "2026-06-01", "completed"), (2, 2, "2026-06-05", "completed"), (3, 1, "2026-07-03", "pending"), (4, 3, "2026-07-12", "completed")])
            connection.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", [(1, 1, 1, 2, 199.0), (2, 2, 2, 1, 99.0), (3, 3, 3, 1, 149.0), (4, 4, 1, 1, 199.0), (5, 4, 2, 3, 99.0)])


def _validate_table(table: str) -> str:
    if table not in TABLES:
        raise ValueError("Unknown table")
    return table


def _validate_query(query: str) -> str:
    cleaned = query.strip()
    if not cleaned.lower().startswith("select"):
        raise ValueError("Only SELECT statements are allowed")
    if ";" in cleaned:
        raise ValueError("Multiple statements are not allowed")
    forbidden = ("insert", "update", "delete", "drop", "alter", "create", "attach", "detach", "pragma")
    if any(word in cleaned.lower() for word in forbidden):
        raise ValueError("Query contains a forbidden operation")
    return cleaned


@mcp.tool()
def sqlite_list_tables() -> dict[str, list[str]]:
    """List available analytical tables."""
    initialize_database()
    return {"tables": sorted(TABLES)}


@mcp.tool()
def sqlite_describe_table(table: str) -> dict[str, object]:
    """Return column and key metadata for one allowed table."""
    initialize_database()
    allowed = _validate_table(table)
    with _connect(read_only=True) as connection:
        columns = connection.execute(f"PRAGMA table_info({allowed})").fetchall()
        foreign_keys = connection.execute(f"PRAGMA foreign_key_list({allowed})").fetchall()
    return {
        "table": allowed,
        "columns": [{"name": row[1], "type": row[2], "nullable": not bool(row[3]), "primary_key": bool(row[5])} for row in columns],
        "foreign_keys": [{"column": row[3], "references_table": row[2], "references_column": row[4]} for row in foreign_keys],
    }


@mcp.tool()
def sqlite_query(query: str) -> dict[str, object]:
    """Execute a validated, read-only analytical SELECT query."""
    initialize_database()
    statement = _validate_query(query)
    with _connect(read_only=True) as connection:
        cursor = connection.execute(statement)
        columns = [description[0] for description in cursor.description or []]
        rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchmany(200)]
    return {"columns": columns, "rows": rows, "truncated": len(rows) == 200}


if __name__ == "__main__":
    initialize_database()
    mcp.run()
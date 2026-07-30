"""
pgvector storage layer for CBAC policy embeddings.
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()

_DEFAULT_DSN = "postgresql://localhost/agentdna"
_SQL_DIR = Path(__file__).parent / "sql"

# Load SQL queries from files
_SQL_SAVE_CHUNKS = (_SQL_DIR / "save_chunks.sql").read_text()
_SQL_UPSERT_META = (_SQL_DIR / "upsert_meta.sql").read_text()
_SQL_GET_META = (_SQL_DIR / "get_meta.sql").read_text()
_SQL_MAX_COSINE = (_SQL_DIR / "max_cosine.sql").read_text()
_SQL_TOP_K = (_SQL_DIR / "top_k_chunks.sql").read_text()


class PolicyStore:
    """Handles all pgvector read/write ops for policy chunks."""

    def __init__(self, dsn: str = ""):
        self._dsn = dsn or os.environ.get("AGENTDNA_DATABASE_URL", "") or _DEFAULT_DSN
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)
            register_vector(self._conn)
        return self._conn

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()

    # ---- writes ----

    def save_policy_chunks(
        self,
        agent_id: str,
        chunks: list[dict[str, Any]],
        policy_hash: str,
        encoder: str,
        nli_model: str,
    ) -> None:
        """Replace all chunks for an agent and update meta."""
        conn = self._get_conn()

        with conn.transaction():
            conn.execute("DELETE FROM policy_chunks WHERE agent_id = %s", (agent_id,))

            with conn.cursor() as cur:
                for chunk in chunks:
                    cur.execute(
                        _SQL_SAVE_CHUNKS,
                        (
                            agent_id,
                            chunk["text"],
                            chunk["type"],
                            chunk["embedding"],
                            policy_hash,
                            chunk.get("section", "body"),
                            chunk.get("index", 0),
                        ),
                    )

            conn.execute(
                _SQL_UPSERT_META,
                (
                    agent_id,
                    policy_hash,
                    encoder,
                    nli_model,
                    len(chunks),
                    datetime.now(timezone.utc),
                ),
            )

    def delete_agent_policy(self, agent_id: str) -> None:
        """Wipe all chunks + meta for an agent."""
        conn = self._get_conn()
        with conn.transaction():
            conn.execute("DELETE FROM policy_chunks WHERE agent_id = %s", (agent_id,))
            conn.execute("DELETE FROM policy_meta WHERE agent_id = %s", (agent_id,))

    # ---- reads ----

    def get_policy_meta(self, agent_id: str) -> Optional[dict[str, Any]]:
        """Fetch meta row for cache validation. None if not cached."""
        conn = self._get_conn()
        return conn.execute(_SQL_GET_META, (agent_id,)).fetchone()

    def policy_exists(self, agent_id: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM policy_meta WHERE agent_id = %s", (agent_id,)
        ).fetchone()
        return row is not None

    def max_cosine_similarity(
        self,
        agent_id: str,
        chunk_type: str,
        query_vector: list[float],
    ) -> tuple[float, str]:
        """Closest chunk by cosine. Returns (score, text) or (0.0, "")."""
        conn = self._get_conn()
        row = conn.execute(
            _SQL_MAX_COSINE,
            (query_vector, agent_id, chunk_type, query_vector),
        ).fetchone()

        if row is None:
            return (0.0, "")
        return (float(row["similarity"]), row["chunk_text"])

    def top_k_chunks(
        self,
        agent_id: str,
        chunk_type: str,
        query_vector: list[float],
        k: int = 5,
    ) -> list[dict[str, Any]]:
        """Top-k nearest chunks with scores."""
        conn = self._get_conn()
        rows = conn.execute(
            _SQL_TOP_K,
            (query_vector, agent_id, chunk_type, query_vector, k),
        ).fetchall()

        return [
            {
                "chunk_text": r["chunk_text"],
                "similarity": float(r["similarity"]),
                "section": r["section"],
                "chunk_index": r["chunk_index"],
            }
            for r in rows
        ]

    def get_all_chunks(
        self,
        agent_id: str,
        chunk_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """All chunks for an agent. Optionally filter by type."""
        conn = self._get_conn()
        if chunk_type:
            rows = conn.execute(
                """
                SELECT chunk_text, chunk_type, section, chunk_index
                FROM policy_chunks
                WHERE agent_id = %s AND chunk_type = %s
                ORDER BY chunk_index
                """,
                (agent_id, chunk_type),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT chunk_text, chunk_type, section, chunk_index
                FROM policy_chunks
                WHERE agent_id = %s
                ORDER BY chunk_type, chunk_index
                """,
                (agent_id,),
            ).fetchall()

        return list(rows)

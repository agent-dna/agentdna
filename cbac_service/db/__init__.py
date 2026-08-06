"""Database package for cbac_service — async SQLAlchemy engine, models, repository, and search."""

from .base import Base
from .engine import close_db, get_session
from .models import PolicyChunk, PolicyMeta
from .repository import (
    delete_policy_chunks,
    get_policy_chunks,
    get_policy_meta,
    policy_hash_matches,
    save_policy_chunks,
    upsert_policy_meta,
)
from .search import (
    SearchResult,
    bm25_search,
    hybrid_search,
    vector_search,
)

__all__ = [
    "Base",
    "PolicyChunk",
    "PolicyMeta",
    "SearchResult",
    "bm25_search",
    "close_db",
    "delete_policy_chunks",
    "get_policy_chunks",
    "get_policy_meta",
    "get_session",
    "hybrid_search",
    "policy_hash_matches",
    "save_policy_chunks",
    "upsert_policy_meta",
    "vector_search",
]

"""Hybrid search functions — pgvector cosine + pg_textsearch BM25 + RRF fusion.

These functions replace the in-memory numpy brute-force approach with
Postgres-native search. They return ranked results that the CBAC decision
pipeline (Tier 1 cosine gap, Tier 2 NLI) consumes directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cbac_service.config import RRF_K

from .models import PolicyChunk


@dataclass
class SearchResult:
    """A single search result with its score and source chunk data."""

    chunk_id: int
    agent_id: str
    chunk_text: str
    chunk_type: str
    score: float  # cosine similarity (0-1) or negative BM25 score
    chunk_index: int
    section: str | None = None


async def vector_search(
    session: AsyncSession,
    agent_id: str,
    query_embedding: np.ndarray,
    top_k: int = 5,
    chunk_type: str | None = None,
) -> list[SearchResult]:
    """Cosine similarity search via pgvector.

    Uses the `<=>` operator (cosine distance). We convert to similarity
    (1 - distance) so higher = more similar, matching the old numpy behavior.

    Parameters
    ----------
    session : async DB session
    agent_id : whose chunks to search
    query_embedding : 384-dim vector (the encoded intent)
    top_k : how many results to return
    chunk_type : 'allowed', 'forbidden', or None for both
    """
    distance = PolicyChunk.embedding.cosine_distance(query_embedding)

    stmt = select(
        PolicyChunk.id,
        PolicyChunk.agent_id,
        PolicyChunk.chunk_text,
        PolicyChunk.chunk_type,
        PolicyChunk.chunk_index,
        PolicyChunk.section,
        (1 - distance).label("similarity"),
    ).where(PolicyChunk.agent_id == agent_id)

    if chunk_type is not None:
        stmt = stmt.where(PolicyChunk.chunk_type == chunk_type)

    stmt = stmt.order_by(distance).limit(top_k)
    result = await session.execute(stmt)

    return [
        SearchResult(
            chunk_id=row.id,
            agent_id=row.agent_id,
            chunk_text=row.chunk_text,
            chunk_type=row.chunk_type,
            score=float(row.similarity),
            chunk_index=row.chunk_index,
            section=row.section,
        )
        for row in result
    ]


async def bm25_search(
    session: AsyncSession,
    agent_id: str,
    query_text: str,
    top_k: int = 5,
    chunk_type: str | None = None,
) -> list[SearchResult]:
    """BM25 keyword search via pg_textsearch.

    Uses the `<@>` operator which returns negative BM25 scores (lower = more
    relevant). We negate the score so higher = more relevant, consistent with
    vector_search.

    Parameters
    ----------
    session : async DB session
    agent_id : whose chunks to search
    query_text : the raw intent text for keyword matching
    top_k : how many results to return
    chunk_type : 'allowed', 'forbidden', or None for both
    """
    type_filter = ""
    if chunk_type is not None:
        type_filter = "AND chunk_type = :chunk_type"

    # pg_textsearch requires explicit index naming via to_bm25query() when
    # the query goes through prepared statements (asyncpg parameterizes).
    # The index name is 'policy_chunks_bm25_idx' as created in the migration.
    query = text(f"""
        SELECT id, agent_id, chunk_text, chunk_type, chunk_index, section,
               -(chunk_text <@> to_bm25query(:query_text, 'policy_chunks_bm25_idx')) AS bm25_score
        FROM policy_chunks
        WHERE agent_id = :agent_id {type_filter}
        ORDER BY chunk_text <@> to_bm25query(:query_text, 'policy_chunks_bm25_idx')
        LIMIT :top_k
    """)

    params: dict = {
        "agent_id": agent_id,
        "query_text": query_text,
        "top_k": top_k,
    }
    if chunk_type is not None:
        params["chunk_type"] = chunk_type

    result = await session.execute(query, params)
    rows = result.fetchall()

    return [
        SearchResult(
            chunk_id=row.id,
            agent_id=row.agent_id,
            chunk_text=row.chunk_text,
            chunk_type=row.chunk_type,
            score=float(row.bm25_score),
            chunk_index=row.chunk_index,
            section=row.section,
        )
        for row in rows
    ]


async def hybrid_search(
    session: AsyncSession,
    agent_id: str,
    query_embedding: np.ndarray,
    query_text: str,
    top_k: int = 5,
    chunk_type: str | None = None,
    rrf_k: int = RRF_K,
) -> list[SearchResult]:
    """Hybrid search: combines vector similarity + BM25 keyword search via RRF.

    Reciprocal Rank Fusion merges two ranked lists into one:
        rrf_score(doc) = 1/(k + rank_vector) + 1/(k + rank_bm25)

    Documents appearing in only one list get a single RRF contribution.
    The fused list is sorted by descending RRF score.

    Parameters
    ----------
    session : async DB session
    agent_id : whose chunks to search
    query_embedding : 384-dim vector for semantic search
    query_text : raw text for keyword search
    top_k : final number of results after fusion
    chunk_type : 'allowed', 'forbidden', or None for both
    rrf_k : RRF constant (default 60, higher = less aggressive re-ranking)
    """
    # Fetch more candidates from each retriever to ensure good fusion coverage.
    fetch_k = top_k * 3

    vector_results = await vector_search(
        session, agent_id, query_embedding, top_k=fetch_k, chunk_type=chunk_type
    )
    bm25_results = await bm25_search(
        session, agent_id, query_text, top_k=fetch_k, chunk_type=chunk_type
    )

    # Build RRF scores keyed by chunk_id.
    rrf_scores: dict[int, float] = {}
    chunk_map: dict[int, SearchResult] = {}

    for rank, result in enumerate(vector_results, start=1):
        rrf_scores[result.chunk_id] = rrf_scores.get(result.chunk_id, 0.0) + 1.0 / (rrf_k + rank)
        chunk_map[result.chunk_id] = result

    for rank, result in enumerate(bm25_results, start=1):
        rrf_scores[result.chunk_id] = rrf_scores.get(result.chunk_id, 0.0) + 1.0 / (rrf_k + rank)
        if result.chunk_id not in chunk_map:
            chunk_map[result.chunk_id] = result

    # Sort by RRF score descending and take top_k.
    sorted_ids = sorted(rrf_scores, key=lambda cid: rrf_scores[cid], reverse=True)[:top_k]

    # Return results with the fused RRF score.
    return [
        SearchResult(
            chunk_id=cid,
            agent_id=chunk_map[cid].agent_id,
            chunk_text=chunk_map[cid].chunk_text,
            chunk_type=chunk_map[cid].chunk_type,
            score=rrf_scores[cid],
            chunk_index=chunk_map[cid].chunk_index,
            section=chunk_map[cid].section,
        )
        for cid in sorted_ids
    ]

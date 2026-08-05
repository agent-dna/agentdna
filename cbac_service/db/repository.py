"""Repository layer — async DB access for the CBAC policy store.

All SQLAlchemy session handling lives here so `cbac.py` stays clean
of ORM concerns. Every function takes an AsyncSession and returns
plain data (models, booleans, or None).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbac_service.config import ENCODER_MODEL, NLI_MODEL

from .models import PolicyChunk, PolicyMeta


async def policy_hash_matches(
    session: AsyncSession,
    agent_id: str,
    policy_hash: str,
) -> bool:
    """Check if the stored policy hash matches the current on-chain hash.

    Returns True if cache is valid (no recompute needed), False otherwise.
    Also returns False if no meta row exists for this agent.
    """
    stmt = select(PolicyMeta.policy_hash).where(PolicyMeta.agent_id == agent_id)
    result = await session.execute(stmt)
    stored_hash = result.scalar_one_or_none()
    if stored_hash is None:
        return False
    return stored_hash == policy_hash


async def save_policy_chunks(
    session: AsyncSession,
    agent_id: str,
    chunks: list[str],
    chunk_types: list[str],
    embeddings: np.ndarray,
    policy_hash: str,
    sections: list[str | None] | None = None,
) -> int:
    """Bulk-write policy chunks for an agent, replacing any existing rows.

    Steps:
      1. Delete all existing chunks for this agent.
      2. Insert new chunks with embeddings.
      3. Upsert the policy_meta row.

    Returns the number of chunks inserted.
    """
    if sections is None:
        sections = [None] * len(chunks)

    # 1. Delete old chunks for this agent.
    await session.execute(
        delete(PolicyChunk).where(PolicyChunk.agent_id == agent_id)
    )

    # 2. Insert new chunks.
    rows = []
    for i, (text, ctype, section) in enumerate(zip(chunks, chunk_types, sections)):
        row = PolicyChunk(
            agent_id=agent_id,
            chunk_text=text,
            chunk_type=ctype,
            embedding=embeddings[i].tolist(),
            policy_hash=policy_hash,
            section=section,
            chunk_index=i,
        )
        rows.append(row)

    session.add_all(rows)

    # 3. Upsert policy_meta.
    await upsert_policy_meta(
        session=session,
        agent_id=agent_id,
        policy_hash=policy_hash,
        encoder_model=ENCODER_MODEL,
        nli_model=NLI_MODEL,
        chunk_count=len(rows),
    )

    await session.commit()
    return len(rows)


async def get_policy_chunks(
    session: AsyncSession,
    agent_id: str,
    chunk_type: str | None = None,
) -> Sequence[PolicyChunk]:
    """Load all policy chunks for an agent, ordered by chunk_index.

    Optionally filter by chunk_type ('allowed' or 'forbidden').
    Embeddings come back as Python lists — convert to numpy in the caller
    if needed.
    """
    stmt = (
        select(PolicyChunk)
        .where(PolicyChunk.agent_id == agent_id)
        .order_by(PolicyChunk.chunk_index)
    )
    if chunk_type is not None:
        stmt = stmt.where(PolicyChunk.chunk_type == chunk_type)

    result = await session.execute(stmt)
    return result.scalars().all()


async def get_policy_meta(
    session: AsyncSession,
    agent_id: str,
) -> PolicyMeta | None:
    """Get the policy meta row for an agent, or None if not cached."""
    stmt = select(PolicyMeta).where(PolicyMeta.agent_id == agent_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def upsert_policy_meta(
    session: AsyncSession,
    agent_id: str,
    policy_hash: str,
    encoder_model: str = ENCODER_MODEL,
    nli_model: str = NLI_MODEL,
    chunk_count: int = 0,
) -> PolicyMeta:
    """Insert or update the policy_meta row for an agent."""
    stmt = select(PolicyMeta).where(PolicyMeta.agent_id == agent_id)
    result = await session.execute(stmt)
    meta = result.scalar_one_or_none()

    if meta is None:
        meta = PolicyMeta(
            agent_id=agent_id,
            policy_hash=policy_hash,
            encoder_model=encoder_model,
            nli_model=nli_model,
            chunk_count=chunk_count,
            cached_at=datetime.now(timezone.utc),
        )
        session.add(meta)
    else:
        meta.policy_hash = policy_hash
        meta.encoder_model = encoder_model
        meta.nli_model = nli_model
        meta.chunk_count = chunk_count
        meta.cached_at = datetime.now(timezone.utc)

    return meta


async def delete_policy_chunks(
    session: AsyncSession,
    agent_id: str,
) -> int:
    """Delete all chunks and meta for an agent. Returns rows deleted."""
    result = await session.execute(
        delete(PolicyChunk).where(PolicyChunk.agent_id == agent_id)
    )
    await session.execute(
        delete(PolicyMeta).where(PolicyMeta.agent_id == agent_id)
    )
    await session.commit()
    return result.rowcount  # type: ignore[return-value]

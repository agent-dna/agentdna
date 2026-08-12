"""ORM models for the CBAC vector/policy store."""

from __future__ import annotations

from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PolicyChunk(Base):
    """One text chunk from an agent's policy, with its embedding vector."""

    __tablename__ = "policy_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_type: Mapped[str] = mapped_column(String, nullable=False)  # 'allowed' | 'forbidden'
    embedding: Mapped[list] = mapped_column(Vector(384), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    section: Mapped[str | None] = mapped_column(String, nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_policy_chunks_agent_hash", "agent_id", "policy_hash"),)

    def __repr__(self) -> str:
        return (
            f"<PolicyChunk(id={self.id}, agent_id={self.agent_id!r}, "
            f"chunk_type={self.chunk_type!r}, chunk_index={self.chunk_index})>"
        )


class PolicyMeta(Base):
    """Lightweight per-agent metadata for cache invalidation.

    One row per agent. Compared against on-chain policy hash at runtime
    to decide whether re-embedding is needed.
    """

    __tablename__ = "policy_meta"

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    encoder_model: Mapped[str] = mapped_column(
        String, nullable=False, default="BAAI/bge-small-en-v1.5"
    )
    nli_model: Mapped[str] = mapped_column(
        String, nullable=False, default="cross-encoder/nli-deberta-v3-small"
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (UniqueConstraint("agent_id", name="uq_policy_meta_agent_id"),)

    def __repr__(self) -> str:
        return (
            f"<PolicyMeta(agent_id={self.agent_id!r}, "
            f"policy_hash={self.policy_hash!r}, chunks={self.chunk_count})>"
        )


class LHIRecord(Base):
    """One LHI trust update — a single completed agent→callee interaction.

    Append-only: the table *is* the trust history the JSON file's per-edge
    ``scores`` list used to approximate. The current trust for an edge
    (agent_id, callee_name, callee_type) is the ``trust`` of its latest row;
    the EMA chain is reconstructable because every row stores the value it
    produced. Replaces trust_scores.json under ~/.agentdna/.
    """

    __tablename__ = "lhi_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    callee_name: Mapped[str] = mapped_column(String, nullable=False)
    callee_type: Mapped[str] = mapped_column(String, nullable=False)  # 'tool' | 'agent' | 'mcp'
    intent_score: Mapped[float] = mapped_column(Float, nullable=False)
    policy_score: Mapped[float] = mapped_column(Float, nullable=False)
    hallucination_score: Mapped[float] = mapped_column(Float, nullable=False)
    output_score: Mapped[float] = mapped_column(Float, nullable=False)
    trust: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Latest-row-per-edge is the hot query; id disambiguates same-timestamp rows.
    __table_args__ = (Index("ix_lhi_records_edge", "agent_id", "callee_name", "callee_type", "id"),)

    def __repr__(self) -> str:
        return (
            f"<LHIRecord(id={self.id}, agent_id={self.agent_id!r}, "
            f"edge={self.callee_name!r}:{self.callee_type!r}, trust={self.trust:.3f})>"
        )

"""add lhi_records - per-interaction trust history

Revision ID: 7b21c9e4d3a8
Revises: 4536f57473af
Create Date: 2026-08-07 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b21c9e4d3a8"
down_revision: str | None = "4536f57473af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "lhi_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("callee_name", sa.String(), nullable=False),
        sa.Column("callee_type", sa.String(), nullable=False),
        sa.Column("intent_score", sa.Float(), nullable=False),
        sa.Column("policy_score", sa.Float(), nullable=False),
        sa.Column("hallucination_score", sa.Float(), nullable=False),
        sa.Column("output_score", sa.Float(), nullable=False),
        sa.Column("trust", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lhi_records_agent_id", "lhi_records", ["agent_id"])
    op.create_index(
        "ix_lhi_records_edge",
        "lhi_records",
        ["agent_id", "callee_name", "callee_type", "id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_lhi_records_edge", table_name="lhi_records")
    op.drop_index("ix_lhi_records_agent_id", table_name="lhi_records")
    op.drop_table("lhi_records")

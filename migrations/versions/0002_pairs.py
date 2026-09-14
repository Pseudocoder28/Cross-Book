"""pairs table — proposed/confirmed cross-venue market equivalences

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pairs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kalshi_market_id", sa.Text(), nullable=False),
        sa.Column("polymarket_market_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="proposed"),
        sa.Column("score", sa.Float(), nullable=False),
        # Both legs' descriptive snapshot at proposal time plus the scoring
        # features, so a reviewer sees exactly what the matcher saw.
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("kalshi_market_id", "polymarket_market_id", name="uq_pairs_legs"),
    )
    op.create_index("ix_pairs_status", "pairs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_pairs_status", table_name="pairs")
    op.drop_table("pairs")

"""paper_trades table — simulated fills from the paper trader

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_trades",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("pair_id", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("qty", sa.BigInteger(), nullable=False),  # 0.0001-contract units
        sa.Column("cost_ticks", sa.BigInteger(), nullable=False),
        sa.Column("fee_ticks", sa.BigInteger(), nullable=False),
        sa.Column("net_ticks", sa.BigInteger(), nullable=False),
        sa.Column("ts_ms", sa.BigInteger(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_paper_trades_run", "paper_trades", ["run_id", "ts_ms"])


def downgrade() -> None:
    op.drop_index("ix_paper_trades_run", table_name="paper_trades")
    op.drop_table("paper_trades")

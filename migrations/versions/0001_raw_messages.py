"""raw_messages table

Revision ID: 0001
Revises:
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("ingest_seq", sa.BigInteger(), nullable=False),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("stream", sa.Text(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("recv_ts_ns", sa.BigInteger(), nullable=False),
        sa.Column("recv_mono_ns", sa.BigInteger(), nullable=False),
        sa.Column(
            "inserted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("run_id", "ingest_seq", name="uq_raw_messages_run_seq"),
    )
    op.create_index("ix_raw_messages_venue_recv_ts_ns", "raw_messages", ["venue", "recv_ts_ns"])


def downgrade() -> None:
    op.drop_index("ix_raw_messages_venue_recv_ts_ns", table_name="raw_messages")
    op.drop_table("raw_messages")

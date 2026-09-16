"""control_actions table — audit log of UI control-plane actions

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "control_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ts_ns", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        # Dotted control name, e.g. "recording.stop" / "pairs.propose".
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False),
        # The human sentence the UI SHOWED the operator before they confirmed.
        # Stored verbatim: the point of the audit is what they agreed to, not
        # what we would re-derive from params today.
        sa.Column("effect", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_control_actions_run", "control_actions", ["run_id", "ts_ns"])
    op.create_index("ix_control_actions_ts_ns", "control_actions", ["ts_ns"])


def downgrade() -> None:
    op.drop_index("ix_control_actions_ts_ns", table_name="control_actions")
    op.drop_index("ix_control_actions_run", table_name="control_actions")
    op.drop_table("control_actions")

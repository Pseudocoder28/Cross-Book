"""pairs.tracked — "watch this now" stops being inferred from score

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-16

``confirmed`` is a judgement about the world ("these two markets resolve to
the same thing"): durable and semantic. Which confirmed pairs are *watched* is
an operational choice bounded by a hard budget — Polymarket US is polled on one
global 0.45 req/s budget, so every tracked pair adds a poll target and ~2.2 s
of staleness to every other Polymarket book. Until now the watched set was
inferred as "top N confirmed by score", which with tied scores degenerates to
``ORDER BY score DESC, id`` — the lowest ids win forever and a pair confirmed
today can never be watched. This column makes the choice explicit state.

The upgrade path for a live database
------------------------------------
Defaulting every existing row to ``false`` would silently stop a running
system: the operator confirmed those pairs and the ARB screen is quoting them
right now. Defaulting all confirmed rows to ``true`` would silently blow the
poll budget (46 confirmed pairs → ~120 s per book, which is not a quote).

So this migration **preserves exactly what the old code was watching**: the
top ``10`` confirmed pairs by ``(score DESC, id)`` — ``10`` because that is the
default of ``--pairs-top`` in ``arb ui``/``arb replay`` and of the control
plane's replay params, i.e. the set that was live at migration time. Nothing
starts being watched, nothing stops being watched, and from here the operator
changes the set from ``/control`` and ``/pairs``. A database whose UI ran with
a different ``--pairs-top`` gets the same 10 and re-picks its set in the UI —
visible and one click away, unlike either silent extreme.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The old default of --pairs-top: what a UI started without the flag watched.
LEGACY_PAIRS_TOP = 10


def upgrade() -> None:
    op.add_column(
        "pairs",
        sa.Column("tracked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Carry the live watch set across the migration (see the docstring).
    op.execute(
        sa.text(
            "UPDATE pairs SET tracked = true WHERE id IN ("
            "  SELECT id FROM pairs WHERE status = 'confirmed'"
            f" ORDER BY score DESC, id LIMIT {LEGACY_PAIRS_TOP}"
            ")"
        )
    )
    # The one hot lookup: "the pairs to watch" = status='confirmed' AND tracked.
    op.create_index("ix_pairs_status_tracked", "pairs", ["status", "tracked"])


def downgrade() -> None:
    op.drop_index("ix_pairs_status_tracked", table_name="pairs")
    op.drop_column("pairs", "tracked")

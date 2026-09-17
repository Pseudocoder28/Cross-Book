"""SQLAlchemy models. Schema changes go through Alembic migrations.

Times are stored in UTC. Nanosecond timestamps are BIGINTs (`*_ns`), never
floats; `recv_mono_ns` is only comparable within one `run_id`.

Models stay dialect-portable — the fast tests create this metadata on SQLite,
while the migrations target Postgres. Dialect-specific SQL lives in the store
modules, not here.

The `control_actions` helpers at the bottom are an exception to "models only":
they are here because this milestone's file set did not include a store
module. They belong in `arb/storage/audit.py` once one exists.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    false,
    func,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite (used by fast tests) only autoincrements INTEGER primary keys;
# Postgres gets a BIGSERIAL either way.
_BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    pass


class RawMessageRow(Base):
    """One raw inbound venue message, exactly as received, pre-parse."""

    __tablename__ = "raw_messages"

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    ingest_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    stream: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    recv_ts_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recv_mono_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("run_id", "ingest_seq", name="uq_raw_messages_run_seq"),
        Index("ix_raw_messages_venue_recv_ts_ns", "venue", "recv_ts_ns"),
    )


class PairRow(Base):
    """A proposed or human-decided equivalence between one Kalshi market and
    one Polymarket US market. ``detail`` snapshots both legs and the scoring
    features at proposal time.

    ``status`` and ``tracked`` are two different decisions. ``status`` is a
    judgement about the world ("these resolve to the same thing"): durable.
    ``tracked`` is "watch this now": operational, reversible, and bounded by
    the Polymarket poll budget, since every tracked pair is one more poll
    target. Confirmed is a precondition for tracked (``pairs.store.decide``
    clears the flag when a pair stops being confirmed), never a synonym.
    """

    __tablename__ = "pairs"

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    kalshi_market_id: Mapped[str] = mapped_column(Text, nullable=False)
    polymarket_market_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    score: Mapped[float] = mapped_column(Float, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    tracked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("kalshi_market_id", "polymarket_market_id", name="uq_pairs_legs"),
        Index("ix_pairs_status", "status"),
        # The one hot lookup: the pairs to watch right now.
        Index("ix_pairs_status_tracked", "status", "tracked"),
    )


class PaperTradeRow(Base):
    """One simulated fill from the paper trader (live or replay)."""

    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    pair_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    qty: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_ticks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_ticks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_ticks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_paper_trades_run", "run_id", "ts_ms"),)


class ControlActionRow(Base):
    """One operator control action from the UI control plane.

    This table is the only record that a gap in recording (or a change to
    limits, or a job that wrote 12k rows) was *deliberate*. ``effect`` is the
    human sentence the UI SHOWED the operator before they confirmed — not a
    re-derivation after the fact — because the thing worth reading back a week
    later is what the operator was told they were agreeing to.
    """

    __tablename__ = "control_actions"

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    ts_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Dotted name of the control, e.g. "recording.stop" or "pairs.propose".
    action: Mapped[str] = mapped_column(Text, nullable=False)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    effect: Mapped[str] = mapped_column(Text, nullable=False)
    # Who asked: "ui", "cli", or a more specific label the caller supplies.
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    # "ok" | "error" | whatever the control layer needs ("armed", "cancelled").
    result: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_control_actions_run", "run_id", "ts_ns"),
        Index("ix_control_actions_ts_ns", "ts_ns"),
    )


def control_action_payload(row: Any) -> dict[str, Any]:
    """JSON-ready shape of one audit row (used by the API and the UI)."""
    return {
        "id": row.id,
        "ts_ns": row.ts_ns,
        "run_id": row.run_id,
        "action": row.action,
        "params": dict(row.params_json or {}),
        "effect": row.effect,
        "actor": row.actor,
        "result": row.result,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def insert_control_action(
    engine: AsyncEngine,
    *,
    run_id: str,
    action: str,
    effect: str,
    actor: str = "ui",
    result: str = "ok",
    params: dict[str, Any] | None = None,
    error: str | None = None,
    ts_ns: int | None = None,
) -> int:
    """Write one audit row and return its id.

    Raises on database failure so a G3 action can fail closed: if we cannot
    record that it happened, it does not happen. Callers for whom the audit is
    advisory must catch and count, never let it propagate into ingest.
    """
    stmt = insert(ControlActionRow).values(
        ts_ns=time.time_ns() if ts_ns is None else ts_ns,
        run_id=run_id,
        action=action,
        params_json=params if params is not None else {},
        effect=effect,
        actor=actor,
        result=result,
        error=error,
    )
    async with engine.begin() as conn:
        written = await conn.execute(stmt)
    return int(written.inserted_primary_key[0])  # pyright: ignore[reportOptionalSubscript]


async def list_control_actions(
    engine: AsyncEngine,
    *,
    run_id: str | None = None,
    action: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Most recent audit rows first. ``limit`` caps rows in SQL."""
    stmt = select(ControlActionRow).order_by(
        ControlActionRow.ts_ns.desc(), ControlActionRow.id.desc()
    )
    if run_id is not None:
        stmt = stmt.where(ControlActionRow.run_id == run_id)
    if action is not None:
        stmt = stmt.where(ControlActionRow.action == action)
    stmt = stmt.limit(limit)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [control_action_payload(r) for r in rows]

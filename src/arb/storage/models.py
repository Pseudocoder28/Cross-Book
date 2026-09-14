"""SQLAlchemy models. Schema changes go through Alembic migrations.

Times are stored in UTC. Nanosecond timestamps are BIGINTs (`*_ns`), never
floats; `recv_mono_ns` is only comparable within one `run_id`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
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
    features at proposal time."""

    __tablename__ = "pairs"

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    kalshi_market_id: Mapped[str] = mapped_column(Text, nullable=False)
    polymarket_market_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    score: Mapped[float] = mapped_column(Float, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("kalshi_market_id", "polymarket_market_id", name="uq_pairs_legs"),
        Index("ix_pairs_status", "status"),
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

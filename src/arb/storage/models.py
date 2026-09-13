"""SQLAlchemy models. Schema changes go through Alembic migrations.

Times are stored in UTC. Nanosecond timestamps are BIGINTs (`*_ns`), never
floats; `recv_mono_ns` is only comparable within one `run_id`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
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

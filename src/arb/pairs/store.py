"""Persistence for pair proposals and human decisions (``pairs`` table)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from arb.pairs.matcher import PairCandidate
from arb.storage.models import PairRow

STATUSES = ("proposed", "confirmed", "rejected")
UPSERT_CHUNK = 2000  # 5 params/row → 10k params, well under the 32,767 cap


def _leg(ref: Any) -> dict[str, Any]:
    return {
        "venue": ref.venue,
        "market_id": ref.market_id,
        "ticker": ref.ticker,
        "outcome": ref.outcome,
        "rules": ref.rules,
        "close_time": ref.close_time.isoformat() if ref.close_time else None,
    }


def candidate_detail(c: PairCandidate) -> dict[str, Any]:
    return {
        "kalshi": {**_leg(c.kalshi), "event_title": c.kalshi_event},
        "polymarket_us": {**_leg(c.polymarket), "event_title": c.polymarket_event},
        "features": c.features,
    }


async def upsert_proposals(engine: AsyncEngine, candidates: Sequence[PairCandidate]) -> int:
    """Insert new proposals; refresh score/detail of existing rows without
    touching a human decision. Returns the number of rows written."""
    if not candidates:
        return 0
    rows = [
        {
            "kalshi_market_id": c.kalshi.market_id,
            "polymarket_market_id": c.polymarket.market_id,
            "status": "proposed",
            "score": c.score,
            "detail": candidate_detail(c),
        }
        for c in candidates
    ]
    written = 0
    async with engine.begin() as conn:
        # asyncpg allows at most 32,767 bind parameters per statement.
        for start in range(0, len(rows), UPSERT_CHUNK):
            chunk = rows[start : start + UPSERT_CHUNK]
            stmt = pg_insert(PairRow).values(chunk)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_pairs_legs",
                set_={"score": stmt.excluded.score, "detail": stmt.excluded.detail},
            )
            result = await conn.execute(stmt)
            written += result.rowcount
    return written


def row_payload(row: PairRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status,
        "score": row.score,
        "kalshi": row.detail.get("kalshi", {}),
        "polymarket_us": row.detail.get("polymarket_us", {}),
        "features": row.detail.get("features", {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


async def list_pairs(engine: AsyncEngine, *, status: str | None = None) -> list[dict[str, Any]]:
    stmt = select(PairRow).order_by(PairRow.score.desc(), PairRow.id)
    if status is not None:
        stmt = stmt.where(PairRow.status == status)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [row_payload(r) for r in rows]  # pyright: ignore[reportArgumentType]


async def decide_many(engine: AsyncEngine, pair_ids: Sequence[int], status: str) -> int:
    """Apply one decision to many pairs (a whole event pairing at once)."""
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    if not pair_ids:
        return 0
    async with engine.begin() as conn:
        result = await conn.execute(
            update(PairRow)
            .where(PairRow.id.in_(list(pair_ids)))
            .values(status=status, decided_at=datetime.now(UTC) if status != "proposed" else None)
        )
    return result.rowcount


async def decide(engine: AsyncEngine, pair_id: int, status: str) -> dict[str, Any] | None:
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    async with engine.begin() as conn:
        await conn.execute(
            update(PairRow)
            .where(PairRow.id == pair_id)
            .values(status=status, decided_at=datetime.now(UTC) if status != "proposed" else None)
        )
        row = (await conn.execute(select(PairRow).where(PairRow.id == pair_id))).first()
    return row_payload(row) if row is not None else None  # pyright: ignore[reportArgumentType]

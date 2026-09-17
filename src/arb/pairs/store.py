"""Persistence for pair proposals and human decisions (``pairs`` table)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select, update
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
        "series_ticker": ref.series_ticker,
        "event_slug": ref.event_slug,
        "fee_coefficient": ref.fee_coefficient,
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
        "tracked": bool(row.tracked),
        "score": row.score,
        "kalshi": row.detail.get("kalshi", {}),
        "polymarket_us": row.detail.get("polymarket_us", {}),
        "features": row.detail.get("features", {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


async def list_pairs(
    engine: AsyncEngine,
    *,
    status: str | None = None,
    tracked: bool | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Stored pairs, best score first. ``limit`` caps rows in SQL — a full
    universe proposal run stores thousands, and most callers want the top few.

    ``tracked`` filters on the watch flag; ``status="confirmed", tracked=True``
    is the watch set and is the only combination the engine ever loads.
    """
    stmt = select(PairRow).order_by(PairRow.score.desc(), PairRow.id)
    if status is not None:
        stmt = stmt.where(PairRow.status == status)
    if tracked is not None:
        stmt = stmt.where(PairRow.tracked == tracked)
    if limit is not None:
        stmt = stmt.limit(limit)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [row_payload(r) for r in rows]  # pyright: ignore[reportArgumentType]


async def get_pair(engine: AsyncEngine, pair_id: int) -> dict[str, Any] | None:
    """One pair by id, or None."""
    async with engine.connect() as conn:
        row = (await conn.execute(select(PairRow).where(PairRow.id == pair_id))).first()
    return row_payload(row) if row is not None else None  # pyright: ignore[reportArgumentType]


async def backfill_event_slugs(engine: AsyncEngine, by_market_id: dict[str, str]) -> int:
    """Fill in each leg's ``event_slug`` from a market_id -> event_slug map.

    Pairs proposed before the matcher captured event slugs carry a leg detail
    with no way to build a venue link. Re-proposing would refresh them, but it
    also re-scores the whole universe; this touches nothing but the missing
    field, leaving status, score and every other detail key alone.
    """
    if not by_market_id:
        return 0
    updated = 0
    async with engine.begin() as conn:
        rows = (await conn.execute(select(PairRow))).all()
        for row in rows:
            detail = dict(row.detail)  # pyright: ignore[reportAttributeAccessIssue]
            changed = False
            for key in ("kalshi", "polymarket_us"):
                leg = dict(detail.get(key) or {})
                if leg.get("event_slug"):
                    continue
                slug = by_market_id.get(leg.get("market_id", ""))
                if slug:
                    leg["event_slug"] = slug
                    detail[key] = leg
                    changed = True
            if changed:
                await conn.execute(
                    update(PairRow)
                    .where(PairRow.id == row.id)  # pyright: ignore[reportAttributeAccessIssue]
                    .values(detail=detail)
                )
                updated += 1
    return updated


def _decision_values(status: str) -> dict[str, Any]:
    """The column values one decision writes.

    Confirmed is a PRECONDITION for tracked, so any decision that is not
    ``confirmed`` clears the watch flag here rather than leaving it to the
    caller: a rejected pair that stayed tracked would keep two markets
    subscribed and keep spending Polymarket poll budget on a pair nobody
    believes in, and the only sign would be a stale row on /pairs.
    """
    values: dict[str, Any] = {
        "status": status,
        "decided_at": datetime.now(UTC) if status != "proposed" else None,
    }
    if status != "confirmed":
        values["tracked"] = False
    return values


async def decide_many(engine: AsyncEngine, pair_ids: Sequence[int], status: str) -> int:
    """Apply one decision to many pairs (a whole event pairing at once)."""
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    if not pair_ids:
        return 0
    async with engine.begin() as conn:
        result = await conn.execute(
            update(PairRow)
            .where(PairRow.id.in_(list(dict.fromkeys(pair_ids))))
            .values(**_decision_values(status))
        )
    return result.rowcount


async def decide(engine: AsyncEngine, pair_id: int, status: str) -> dict[str, Any] | None:
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    async with engine.begin() as conn:
        await conn.execute(
            update(PairRow).where(PairRow.id == pair_id).values(**_decision_values(status))
        )
        row = (await conn.execute(select(PairRow).where(PairRow.id == pair_id))).first()
    return row_payload(row) if row is not None else None  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# tracked: which confirmed pairs are watched right now
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PairFlags:
    """A pair without its ``detail`` blob.

    Deciding what to track needs the ids, the two legs and the two flags, and
    nothing else — a full proposal run's ``detail`` column is ~17MB, which is
    not a price worth paying to count rows or to price a poll cycle.
    """

    id: int
    status: str
    tracked: bool
    score: float
    kalshi_market_id: str
    polymarket_market_id: str

    @property
    def kalshi_ticker(self) -> str:
        return self.kalshi_market_id.partition(":")[2]

    @property
    def polymarket_slug(self) -> str:
        """The poll target this pair costs (market ids are ``venue:native``)."""
        return self.polymarket_market_id.partition(":")[2]


async def pair_flags(
    engine: AsyncEngine,
    *,
    pair_ids: Sequence[int] | None = None,
    status: str | None = None,
    tracked: bool | None = None,
) -> list[PairFlags]:
    """Light rows, best score first, for pricing and validating a track change."""
    if pair_ids is not None and not pair_ids:
        return []
    stmt = select(
        PairRow.id,
        PairRow.status,
        PairRow.tracked,
        PairRow.score,
        PairRow.kalshi_market_id,
        PairRow.polymarket_market_id,
    ).order_by(PairRow.score.desc(), PairRow.id)
    if pair_ids is not None:
        stmt = stmt.where(PairRow.id.in_(list(dict.fromkeys(pair_ids))))
    if status is not None:
        stmt = stmt.where(PairRow.status == status)
    if tracked is not None:
        stmt = stmt.where(PairRow.tracked == tracked)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [
        PairFlags(
            id=int(r.id),
            status=str(r.status),
            tracked=bool(r.tracked),
            score=float(r.score),
            kalshi_market_id=str(r.kalshi_market_id),
            polymarket_market_id=str(r.polymarket_market_id),
        )
        for r in rows
    ]


async def counts(engine: AsyncEngine) -> dict[str, int]:
    """``{"total", "confirmed", "tracked"}`` in one aggregate.

    The gap between ``tracked`` and ``confirmed`` is the number the operator
    needs on /control: confirming is free, tracking is not.
    """
    one_if = lambda cond: func.sum(case((cond, 1), else_=0))  # noqa: E731
    stmt = select(
        func.count(PairRow.id),
        one_if(PairRow.status == "confirmed"),
        one_if(PairRow.tracked),
    )
    async with engine.connect() as conn:
        row = (await conn.execute(stmt)).one()
    return {
        "total": int(row[0] or 0),
        "confirmed": int(row[1] or 0),
        "tracked": int(row[2] or 0),
    }


async def set_tracked(engine: AsyncEngine, pair_ids: Sequence[int], tracked: bool) -> int:
    """Set (or clear) the watch flag. Returns rows actually CHANGED.

    Changed, not matched: "10 rows already looked like that" and "10 rows were
    updated" are the same success to a caller that counts matches, and that is
    exactly how a control action came to report success while doing nothing.

    Tracking is refused for a pair that is not confirmed — enforced here, in
    SQL, so no caller can create a tracked-but-unconfirmed row by forgetting.
    """
    ids = list(dict.fromkeys(pair_ids))
    if not ids:
        return 0
    stmt = (
        update(PairRow)
        .where(PairRow.id.in_(ids), PairRow.tracked != tracked)
        .values(tracked=tracked)
    )
    if tracked:
        stmt = stmt.where(PairRow.status == "confirmed")
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
    return result.rowcount


async def set_tracked_exact(engine: AsyncEngine, pair_ids: Sequence[int]) -> tuple[int, int]:
    """Make the watch set exactly ``pair_ids``, in ONE transaction.

    Returns ``(newly tracked, newly untracked)``. One transaction because a
    bulk setter that half-applied would leave the poll budget spent on a set
    the operator never chose.
    """
    ids = list(dict.fromkeys(pair_ids))
    async with engine.begin() as conn:
        added = 0
        if ids:
            added = (
                await conn.execute(
                    update(PairRow)
                    .where(
                        PairRow.id.in_(ids),
                        PairRow.status == "confirmed",
                        PairRow.tracked.is_(False),
                    )
                    .values(tracked=True)
                )
            ).rowcount
        drop = update(PairRow).where(PairRow.tracked.is_(True)).values(tracked=False)
        if ids:
            drop = drop.where(PairRow.id.not_in(ids))
        removed = (await conn.execute(drop)).rowcount
    return added, removed

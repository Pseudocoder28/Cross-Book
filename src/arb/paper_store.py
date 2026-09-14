"""Persistence for paper trades."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from arb.paper import PaperTrade
from arb.storage.models import PaperTradeRow


async def insert_trades(engine: AsyncEngine, run_id: str, trades: Sequence[PaperTrade]) -> int:
    if not trades:
        return 0
    rows = [
        {
            "run_id": run_id,
            "pair_id": t.pair_id,
            "direction": t.direction,
            "qty": t.qty,
            "cost_ticks": t.cost_ticks,
            "fee_ticks": t.fee_ticks,
            "net_ticks": t.net_ticks,
            "ts_ms": t.ts_ms,
            "detail": t.payload(),
        }
        for t in trades
    ]
    async with engine.begin() as conn:
        result = await conn.execute(insert(PaperTradeRow), rows)
    return result.rowcount


async def list_trades(
    engine: AsyncEngine, *, run_id: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    stmt = select(PaperTradeRow).order_by(PaperTradeRow.ts_ms.desc(), PaperTradeRow.id.desc())
    if run_id is not None:
        stmt = stmt.where(PaperTradeRow.run_id == run_id)
    stmt = stmt.limit(limit)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = dict(r.detail)  # pyright: ignore[reportAttributeAccessIssue]
        payload["id"] = r.id  # pyright: ignore[reportAttributeAccessIssue]
        payload["run_id"] = r.run_id  # pyright: ignore[reportAttributeAccessIssue]
        out.append(payload)
    return out

"""Engine construction and the raw-message sink used by the recorder."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from arb.storage.models import RawMessageRow
from arb.types import RawMessage


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


async def insert_raw_messages(engine: AsyncEngine, batch: Sequence[RawMessage]) -> None:
    """Bulk-insert one recorder batch in a single transaction."""
    if not batch:
        return
    rows = [
        {
            "run_id": m.run_id,
            "ingest_seq": m.ingest_seq,
            "venue": m.venue,
            "stream": m.stream,
            "payload": m.payload,
            "recv_ts_ns": m.recv_ts_ns,
            "recv_mono_ns": m.recv_mono_ns,
        }
        for m in batch
    ]
    async with engine.begin() as conn:
        await conn.execute(insert(RawMessageRow), rows)

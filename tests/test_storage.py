"""Storage round-trip against in-memory SQLite (schema portability is kept in
mind: dialect-specific SQL stays out of the models). Postgres integration runs
against the compose stack once `arb doctor` / recording land."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from arb.storage.db import insert_raw_messages
from arb.storage.models import Base, RawMessageRow
from arb.types import RawMessage


def msg(i: int) -> RawMessage:
    return RawMessage(
        venue="testvenue",
        stream="ws",
        payload=f"payload-{i}".encode(),
        recv_ts_ns=1_000 + i,
        recv_mono_ns=2_000 + i,
        run_id="testrun",
        ingest_seq=i,
    )


async def make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def test_insert_and_read_back() -> None:
    engine = await make_engine()
    try:
        await insert_raw_messages(engine, [msg(0), msg(1)])
        async with engine.connect() as conn:
            rows = (
                await conn.execute(select(RawMessageRow).order_by(RawMessageRow.ingest_seq))
            ).all()
        assert [(r.run_id, r.ingest_seq, r.payload) for r in rows] == [
            ("testrun", 0, b"payload-0"),
            ("testrun", 1, b"payload-1"),
        ]
        assert all(r.venue == "testvenue" and r.stream == "ws" for r in rows)
        assert all(r.inserted_at is not None for r in rows)
    finally:
        await engine.dispose()


async def test_duplicate_run_seq_is_rejected() -> None:
    engine = await make_engine()
    try:
        await insert_raw_messages(engine, [msg(0)])
        with pytest.raises(IntegrityError):
            await insert_raw_messages(engine, [msg(0)])
    finally:
        await engine.dispose()


async def test_empty_batch_is_a_noop() -> None:
    engine = await make_engine()
    try:
        await insert_raw_messages(engine, [])
    finally:
        await engine.dispose()

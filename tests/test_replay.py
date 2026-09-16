"""Replay rebuilds books deterministically from recorded raw messages (real
captured frames loaded into an in-memory database)."""

from pathlib import Path

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from arb.books import BookManager
from arb.replay import replay_run
from arb.storage.models import Base, RawMessageRow

FIX = Path(__file__).parent / "fixtures"


async def seed():  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    frames = [
        line
        for line in (FIX / "kalshi" / "ws_orderbook_capture.jsonl").read_bytes().split(b"\n")
        if line.strip()
    ]
    rows = [
        {
            "run_id": "r1",
            "ingest_seq": i,
            "venue": "kalshi",
            "stream": "ws",
            "payload": frame,
            "recv_ts_ns": 1_700_000_000_000_000_000 + i * 1_000_000,
            "recv_mono_ns": 1_000_000 * i,
        }
        for i, frame in enumerate(frames)
    ]
    pm = (FIX / "polymarket_us" / "rest_book_tec-mlb-nlchamp-2026-09-27-nym.json").read_bytes()
    rows.append(
        {
            "run_id": "r1",
            "ingest_seq": len(frames),
            "venue": "polymarket_us",
            "stream": "rest:book",
            "payload": pm,
            "recv_ts_ns": 1_700_000_000_000_000_000,
            "recv_mono_ns": 1_000_000 * len(frames),
        }
    )
    rows.append(
        {
            "run_id": "r1",
            "ingest_seq": len(frames) + 1,
            "venue": "kalshi",
            "stream": "rest:events",
            "payload": b"{}",
            "recv_ts_ns": 1,
            "recv_mono_ns": 1,
        }
    )
    async with engine.begin() as conn:
        await conn.execute(insert(RawMessageRow), rows)
    return engine, len(frames)


async def test_replay_rebuilds_books_from_recorded_run() -> None:
    engine, n = await seed()
    try:
        books = BookManager(staleness_limit_ns=10**15)
        report = await replay_run(engine, "r1", books=books)
    finally:
        await engine.dispose()
    assert report.messages == n + 2
    assert report.by_stream == {
        "kalshi/ws": n,
        "polymarket_us/rest:book": 1,
        "kalshi/rest:events": 1,
    }
    assert report.skipped == 1 and report.parse_errors == 0
    assert report.books == 6  # five Kalshi markets from the capture + one Polymarket book
    kalshi = [m for m in books.books if m.startswith("kalshi:")]
    assert len(kalshi) == 5 and all(books.get(m) is not None for m in kalshi)
    assert not report.invalid_books  # everything valid at the end of the run
    assert "replay r1" in report.summary()


async def test_missing_run_raises_a_catchable_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # This used to be SystemExit — a BaseException that Starlette's error
    # middleware does not catch and that anyio treats as "shut down".
    from arb import replay
    from arb.config import AppConfig
    from arb.replay import NoRecordedRuns, ReplayError, latest_run_id, run_replay

    assert issubclass(ReplayError, Exception)
    assert not issubclass(ReplayError, SystemExit)

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    assert await latest_run_id(engine) is None
    monkeypatch.setattr(replay, "make_engine", lambda url: engine)

    config = AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
    # A plain `except Exception` handler — all an HTTP route or a job has —
    # sees it, and it carries the same message the CLI used to print.
    try:
        await run_replay(config, None, pairs_top=0)
    except Exception as exc:
        assert isinstance(exc, NoRecordedRuns)
        assert "no recorded runs" in str(exc)
    else:  # pragma: no cover - the call above always raises
        raise AssertionError("expected NoRecordedRuns")

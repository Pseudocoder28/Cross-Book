"""``arb replay``: feed a recorded run back through the identical pipeline.

Raw messages for a ``run_id`` are read from ``raw_messages`` in ``ingest_seq``
order and dispatched to the same adapters and :class:`BookManager` the live
system uses, with the recorded ``recv_mono_ns`` as the clock — so books,
invalidations and edges are reconstructed deterministically. Optionally the
same :class:`ArbMonitor` and :class:`PaperTrader` run on top, which is how a
strategy is evaluated on history before any real money moves.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from arb import paper_store
from arb.arbmon import ArbMonitor
from arb.books import BookManager
from arb.config import AppConfig
from arb.interfaces import ParseError
from arb.pairs.tracked import load_tracked_pairs
from arb.paper import PaperLimits, PaperTrader
from arb.run import RunContext
from arb.storage.db import make_engine
from arb.storage.models import RawMessageRow
from arb.types import RawMessage
from arb.venues.kalshi.adapter import KalshiMarketDataAdapter
from arb.venues.polymarket_us.adapter import PolymarketUSMarketDataAdapter

log = logging.getLogger(__name__)

BATCH = 2000


class ReplayError(Exception):
    """A replay could not be run as asked (bad or missing run id).

    Deliberately a plain ``Exception`` and not ``SystemExit``: this is raised
    from a coroutine that an HTTP handler or a supervised job may await, and
    ``SystemExit`` is a ``BaseException`` — Starlette's error middleware does
    not catch it, ``asyncio``/``anyio`` task groups propagate it as a shutdown
    request, and "no recorded runs yet" would take the server down instead of
    returning a 4xx. Callers that are a CLI turn it into an exit status.
    """


class NoRecordedRuns(ReplayError):
    """``raw_messages`` holds no run to replay."""


@dataclass
class PairStats:
    label: str
    quotes: int = 0
    with_edge: int = 0
    max_net_per_contract_ticks: int = 0
    max_qty: int = 0
    last_net_per_contract_ticks: int = 0

    def payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "quotes": self.quotes,
            "with_edge": self.with_edge,
            "max_net_per_contract_ticks": self.max_net_per_contract_ticks,
            "max_qty": self.max_qty,
            "last_net_per_contract_ticks": self.last_net_per_contract_ticks,
        }


@dataclass
class ReplayReport:
    run_id: str
    messages: int = 0
    by_stream: Counter[str] = field(default_factory=Counter)
    parse_errors: int = 0
    skipped: int = 0
    books: int = 0
    invalid_books: Counter[str] = field(default_factory=Counter)
    pair_stats: dict[int, PairStats] = field(default_factory=dict)
    paper: dict[str, Any] | None = None

    def summary(self) -> str:
        lines = [
            f"replay {self.run_id}: {self.messages} messages, {self.parse_errors} parse errors, "
            f"{self.skipped} non-book messages skipped",
            "streams: " + ", ".join(f"{k}={v}" for k, v in sorted(self.by_stream.items())),
            f"books: {self.books}"
            + (
                " (final invalid: "
                + ", ".join(f"{k}={v}" for k, v in self.invalid_books.items())
                + ")"
                if self.invalid_books
                else ""
            ),
        ]
        if self.pair_stats:
            lines.append(
                f"{'PAIR':<52} {'QUOTES':>6} {'W/EDGE':>6} {'MAX NET/CT':>10} {'MAX SIZE':>9}"
            )
            for ps in sorted(self.pair_stats.values(), key=lambda p: -p.max_net_per_contract_ticks):
                lines.append(
                    f"{ps.label[:52]:<52} {ps.quotes:>6} {ps.with_edge:>6} "
                    f"{ps.max_net_per_contract_ticks / 100:>+9.2f}¢ {ps.max_qty / 10_000:>9.0f}"
                )
        if self.paper is not None:
            t = self.paper["totals"]
            lines.append(
                f"paper: {t['trades']} trades, {t['qty'] / 10_000:.1f} contracts, "
                f"cost ${t['cost_ticks'] / 10_000:.2f}, fees ${t['fee_ticks'] / 10_000:.2f}, "
                f"expected net ${t['net_ticks'] / 10_000:.2f}"
            )
        return "\n".join(lines)


async def replay_run(
    engine: AsyncEngine,
    run_id: str,
    *,
    books: BookManager,
    arbmon: ArbMonitor | None = None,
    trader: PaperTrader | None = None,
    on_message: Callable[[RawMessage], None] | None = None,
) -> ReplayReport:
    report = ReplayReport(run_id=run_id)
    kalshi = KalshiMarketDataAdapter()
    polymarket = PolymarketUSMarketDataAdapter()
    if arbmon is not None:
        for pair in arbmon.pairs:
            report.pair_stats[pair.pair_id] = PairStats(label=pair.label)

    stmt = (
        select(RawMessageRow)
        .where(RawMessageRow.run_id == run_id)
        .order_by(RawMessageRow.ingest_seq)
        .execution_options(yield_per=BATCH)
    )
    async with engine.connect() as conn:
        result = await conn.stream(stmt)
        async for row in result:
            raw = RawMessage(
                venue=row.venue,
                stream=row.stream,
                payload=bytes(row.payload),
                recv_ts_ns=row.recv_ts_ns,
                recv_mono_ns=row.recv_mono_ns,
                run_id=row.run_id,
                ingest_seq=row.ingest_seq,
            )
            report.messages += 1
            report.by_stream[f"{raw.venue}/{raw.stream}"] += 1
            if on_message is not None:
                on_message(raw)
            if raw.venue == "kalshi" and raw.stream == "ws":
                adapter = kalshi
            elif raw.venue == "polymarket_us" and raw.stream == "rest:book":
                adapter = polymarket
            else:
                report.skipped += 1
                continue
            try:
                events = adapter.parse(raw)
            except ParseError:
                report.parse_errors += 1
                continue
            if not events:
                continue
            changed = books.apply(events, mono_ns=raw.recv_mono_ns)
            if arbmon is None or not changed:
                continue
            ts_ms = raw.recv_ts_ns // 1_000_000
            for pair in arbmon.affected(changed):
                d1, d2 = arbmon.best_quotes(pair)
                best = d1 if d1.net_per_contract_ticks >= d2.net_per_contract_ticks else d2
                ps = report.pair_stats[pair.pair_id]
                ps.quotes += 1
                ps.last_net_per_contract_ticks = best.net_per_contract_ticks if best.qty else 0
                if best.qty:
                    ps.with_edge += 1
                    ps.max_net_per_contract_ticks = max(
                        ps.max_net_per_contract_ticks, best.net_per_contract_ticks
                    )
                    ps.max_qty = max(ps.max_qty, best.qty)
                if trader is not None:
                    trader.trade_pair(arbmon, pair, ts_ms=ts_ms, now_mono_ns=raw.recv_mono_ns)
    report.books = len(books.books)
    last_mono = max(
        (m for m in (books.last_update_mono_ns(mid) for mid in books.books) if m), default=0
    )
    for book in books.books.values():
        status = book.status(now_mono_ns=last_mono)
        if not status.valid and status.reason is not None:
            report.invalid_books[status.reason.value] += 1
    if trader is not None:
        report.paper = trader.payload()
    return report


async def latest_run_id(engine: AsyncEngine) -> str | None:
    stmt = (
        select(RawMessageRow.run_id, func.max(RawMessageRow.recv_ts_ns).label("last"))
        .group_by(RawMessageRow.run_id)
        .order_by(func.max(RawMessageRow.recv_ts_ns).desc())
        .limit(1)
    )
    async with engine.connect() as conn:
        row = (await conn.execute(stmt)).first()
    return str(row[0]) if row is not None else None


async def run_replay(
    config: AppConfig,
    run_id: str | None,
    *,
    pairs_top: int = 10,
    paper: bool = False,
    limits: PaperLimits | None = None,
    persist: bool = False,
) -> ReplayReport:
    """Rebuild books for a recorded run; optionally quote confirmed pairs
    (fee parameters fetched live, not recorded) and paper-trade them.

    Raises :class:`ReplayError` when the run cannot be identified. Safe to
    await from a request handler or a job: nothing here raises a
    ``BaseException`` of its own.
    """
    engine = make_engine(config.database_url)
    try:
        if run_id is None:
            run_id = await latest_run_id(engine)
            if run_id is None:
                raise NoRecordedRuns("no recorded runs in raw_messages")
        books = BookManager(staleness_limit_ns=config.book_staleness_limit_ms * 1_000_000)
        books.set_venue_staleness("polymarket_us", 120 * 1_000_000_000)
        arbmon: ArbMonitor | None = None
        if pairs_top > 0:
            load = await load_tracked_pairs(
                config, RunContext(), engine, top_n=pairs_top, sink=None
            )
            if load.tracked:
                arbmon = ArbMonitor(books, load.tracked)
        trader = PaperTrader(limits or PaperLimits()) if paper else None
        report = await replay_run(engine, run_id, books=books, arbmon=arbmon, trader=trader)
        if persist and trader is not None and trader.trades:
            await paper_store.insert_trades(engine, f"replay:{run_id}", trader.trades)
        return report
    finally:
        await engine.dispose()

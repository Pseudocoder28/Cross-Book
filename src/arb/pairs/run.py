"""``arb pairs`` runtime: fetch both universes (recorded), score, persist."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial
from typing import Any

from arb.config import AppConfig
from arb.pairs.matcher import PairCandidate, propose_pairs
from arb.pairs.store import upsert_proposals
from arb.recorder import Recorder
from arb.run import RunContext
from arb.storage.db import insert_raw_messages, make_engine
from arb.supervise import supervise
from arb.venues.kalshi import discovery as kalshi_discovery
from arb.venues.polymarket_us import discovery as pm_discovery

log = logging.getLogger(__name__)

DRAIN_TIMEOUT_S = 15.0


async def propose(
    config: AppConfig,
    *,
    min_score: float,
    record: bool = True,
    kalshi_pages: int = 80,
    pm_pages: int = 12,
) -> list[PairCandidate]:
    """Fetch both venues' open universes, propose pairs, upsert them."""
    run = RunContext(config.run_id or None)
    engine = make_engine(config.database_url)
    recorder: Recorder | None = None
    writer: asyncio.Task[object] | None = None
    if record:
        recorder = Recorder(partial(insert_raw_messages, engine))
        writer = asyncio.create_task(supervise(recorder.run, name="recorder-writer"))
    sink = recorder.enqueue if recorder is not None else None
    try:
        kalshi_pairs = await kalshi_discovery.fetch_universe(
            config, run, sink=sink, max_pages=kalshi_pages
        )
        kalshi_refs = kalshi_discovery.event_refs(kalshi_pairs)
        log.info("kalshi universe: %d events", len(kalshi_refs))
        pm_markets = await pm_discovery.fetch_active_markets(
            config, run, sink=sink, max_pages=pm_pages
        )
        pm_refs = pm_discovery.event_refs(pm_markets)
        log.info("polymarket_us universe: %d events", len(pm_refs))
        candidates = propose_pairs(kalshi_refs, pm_refs, min_score=min_score)
        written = await upsert_proposals(engine, candidates)
        log.info("proposed %d pairs (%d rows written)", len(candidates), written)
        return candidates
    finally:
        if recorder is not None and writer is not None:
            if not await recorder.drain(DRAIN_TIMEOUT_S):
                log.warning("recorder drain timed out")
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        await engine.dispose()


def format_candidates(candidates: list[PairCandidate], *, limit: int = 40) -> str:
    lines = [f"{'SCORE':>5}  {'KALSHI':<34} {'POLYMARKET US':<40} OUTCOME"]
    for c in candidates[:limit]:
        lines.append(
            f"{c.score:5.2f}  {c.kalshi.ticker[:34]:<34} {c.polymarket.ticker[:40]:<40} "
            f"{c.kalshi.outcome[:24]} ↔ {c.polymarket.outcome[:24]}"
        )
    if len(candidates) > limit:
        lines.append(f"… {len(candidates) - limit} more")
    return "\n".join(lines)


def format_rows(rows: list[dict[str, Any]]) -> str:
    lines = [f"{'ID':>5} {'STATUS':<9} {'SCORE':>5}  {'KALSHI':<34} {'POLYMARKET US':<40}"]
    for r in rows:
        k_ticker = r["kalshi"].get("ticker", "")[:34]
        p_ticker = r["polymarket_us"].get("ticker", "")[:40]
        lines.append(
            f"{r['id']:5} {r['status']:<9} {r['score']:5.2f}  {k_ticker:<34} {p_ticker:<40}"
        )
    return "\n".join(lines)

"""``arb pairs`` runtime: fetch both universes (recorded), score, persist."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial
from typing import Any

from arb.config import AppConfig
from arb.pairs import store as pairs_store
from arb.pairs.matcher import PairCandidate, propose_pairs
from arb.pairs.store import upsert_proposals
from arb.recorder import Recorder
from arb.run import RunContext
from arb.storage.db import insert_raw_messages, make_engine
from arb.supervise import supervise
from arb.venues.kalshi import discovery as kalshi_discovery
from arb.venues.kalshi.rest import market_id as kalshi_market_id
from arb.venues.polymarket_us import discovery as pm_discovery
from arb.venues.polymarket_us.rest import event_url as pm_event_url
from arb.venues.polymarket_us.rest import market_id as pm_market_id

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


def _widths(pairs: list[tuple[str, str]], headers: tuple[str, str]) -> tuple[int, int]:
    """Column widths that fit every identifier in full.

    Tickers and slugs are *identifiers*: a reader copies them into a venue
    search box or an API call, so a truncated one is worse than useless — it
    looks real and resolves to nothing. Descriptive text may be elided;
    identifiers never are.
    """
    k = max([len(headers[0]), *(len(a) for a, _ in pairs)] or [len(headers[0])])
    p = max([len(headers[1]), *(len(b) for _, b in pairs)] or [len(headers[1])])
    return k, p


def _elide(text: str, width: int) -> str:
    """Shorten descriptive (non-identifier) text, marking that it was cut."""
    return text if len(text) <= width else text[: width - 1] + "…"


def format_candidates(candidates: list[PairCandidate], *, limit: int = 40) -> str:
    shown = candidates[:limit]
    kw, pw = _widths(
        [(c.kalshi.ticker, c.polymarket.ticker) for c in shown], ("KALSHI", "POLYMARKET US")
    )
    lines = [f"{'SCORE':>5}  {'KALSHI':<{kw}} {'POLYMARKET US':<{pw}} OUTCOME"]
    for c in shown:
        lines.append(
            f"{c.score:5.2f}  {c.kalshi.ticker:<{kw}} {c.polymarket.ticker:<{pw}} "
            f"{_elide(c.kalshi.outcome, 24)} ↔ {_elide(c.polymarket.outcome, 24)}"
        )
    if len(candidates) > limit:
        lines.append(f"… {len(candidates) - limit} more")
    return "\n".join(lines)


def format_rows(rows: list[dict[str, Any]]) -> str:
    legs = [(r["kalshi"].get("ticker", ""), r["polymarket_us"].get("ticker", "")) for r in rows]
    kw, pw = _widths(legs, ("KALSHI", "POLYMARKET US"))
    lines = [f"{'ID':>5} {'STATUS':<9} {'SCORE':>5}  {'KALSHI':<{kw}} {'POLYMARKET US':<{pw}}"]
    for r, (k_ticker, p_ticker) in zip(rows, legs, strict=True):
        lines.append(
            f"{r['id']:5} {r['status']:<9} {r['score']:5.2f}  {k_ticker:<{kw}} {p_ticker:<{pw}}"
        )
    return "\n".join(lines)


def format_pair_detail(row: dict[str, Any]) -> str:
    """Everything about one pair, with identifiers printed in full.

    Exists because a table row cannot carry what a human needs to actually
    look a market up: neither venue's website matches a market slug/ticker in
    its search box, so this prints the *event title* (which does match) and,
    for Polymarket US, the verified event URL.
    """
    k, p = row["kalshi"], row["polymarket_us"]
    out = [
        f"PAIR {row['id']}  [{row['status']}]  score {row['score']:.4f}",
        "",
    ]
    for label, leg in (("KALSHI", k), ("POLYMARKET US", p)):
        out.append(f"{label}")
        out.append(f"  market id   {leg.get('market_id', '')}")
        out.append(f"  ticker      {leg.get('ticker', '')}")
        if leg.get("event_slug"):
            out.append(f"  event       {leg['event_slug']}")
        out.append(f"  event title {leg.get('event_title', '')}")
        out.append(f"  outcome     {leg.get('outcome', '')}")
        if leg.get("close_time"):
            out.append(f"  closes      {leg['close_time']}")
        url = (
            pm_event_url(leg.get("event_slug", "")) if leg.get("venue") == "polymarket_us" else None
        )
        if url:
            out.append(f"  url         {url}")
        elif leg.get("venue") == "polymarket_us":
            # Pre-existing rows were stored before event_slug was captured.
            out.append("  url         (re-run `arb pairs propose` to record the event slug)")
        rules = (leg.get("rules") or "").strip()
        if rules:
            out.append(f"  rules       {_elide(rules, 300)}")
        out.append("")
    feats = row.get("features") or {}
    if feats:
        out.append("FEATURES  " + "  ".join(f"{name}={value}" for name, value in feats.items()))
    out.append(
        "\nNote: neither venue's site searches by slug/ticker — search the event title above,"
        "\nor open the url."
    )
    return "\n".join(out)


async def backfill(config: AppConfig, *, record: bool = False) -> int:
    """Record event slugs onto pairs proposed before the matcher captured them.

    Fetches both universes exactly as :func:`propose` does, but writes only
    the missing ``event_slug`` on each leg — no re-scoring, no new rows, and
    no human decision is touched.
    """
    run = RunContext(config.run_id or None)
    engine = make_engine(config.database_url)
    recorder: Recorder | None = None
    writer: asyncio.Task[object] | None = None
    if record:
        recorder = Recorder(partial(insert_raw_messages, engine))
        writer = asyncio.create_task(supervise(recorder.run, name="recorder-writer"))
    sink = recorder.enqueue if recorder is not None else None
    try:
        by_market_id: dict[str, str] = {}
        for event, markets in await kalshi_discovery.fetch_universe(config, run, sink=sink):
            for m in markets:
                by_market_id[kalshi_market_id(m.ticker)] = event.event_ticker
        for dm in await pm_discovery.fetch_active_markets(config, run, sink=sink):
            by_market_id[pm_market_id(dm.slug)] = dm.event.slug
        log.info("backfill: resolved %d market -> event slugs", len(by_market_id))
        updated = await pairs_store.backfill_event_slugs(engine, by_market_id)
        log.info("backfill: updated %d pairs", updated)
        return updated
    finally:
        if recorder is not None and writer is not None:
            if not await recorder.drain(DRAIN_TIMEOUT_S):
                log.warning("recorder drain timed out")
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        await engine.dispose()

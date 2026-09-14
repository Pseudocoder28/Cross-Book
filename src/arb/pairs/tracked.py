"""Load confirmed pairs as :class:`TrackedPair`s with both venues' fee
parameters resolved from the documented endpoints (recorded before parsing).

Shared by ``arb ui`` (live ARB screen) and ``arb replay`` so both quote with
identical fee models.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from functools import partial

from sqlalchemy.ext.asyncio import AsyncEngine

from arb.arbmon import TrackedPair
from arb.config import AppConfig
from arb.fees import kalshi_fee_ticks, polymarket_fee_ticks
from arb.pairs import store as pairs_store
from arb.run import RunContext
from arb.types import RawMessage
from arb.venues.kalshi.discovery import fetch_event, fetch_market, fetch_series
from arb.venues.kalshi.rest import KalshiEvent, KalshiMarket, KalshiSeries
from arb.venues.polymarket_us.discovery import fetch_markets_by_slug
from arb.venues.polymarket_us.rest import PolymarketUSMarket

log = logging.getLogger(__name__)


@dataclass
class TrackedLoad:
    tracked: list[TrackedPair] = field(default_factory=list)
    kalshi_tickers: list[str] = field(default_factory=list)
    polymarket_slugs: list[str] = field(default_factory=list)
    kalshi_details: list[tuple[KalshiMarket, KalshiEvent]] = field(default_factory=list)
    polymarket_markets: dict[str, PolymarketUSMarket] = field(default_factory=dict)


async def load_tracked_pairs(
    config: AppConfig,
    run: RunContext,
    engine: AsyncEngine,
    *,
    top_n: int,
    sink: Callable[[RawMessage], object] | None = None,
) -> TrackedLoad:
    """Top ``top_n`` confirmed pairs by score, fee parameters resolved."""
    out = TrackedLoad()
    rows = (await pairs_store.list_pairs(engine, status="confirmed"))[:top_n]
    if not rows:
        return out
    pm_by_slug = {
        m.slug: m
        for m in await fetch_markets_by_slug(
            config, run, [r["polymarket_us"]["ticker"] for r in rows], sink=sink
        )
    }
    series_cache: dict[str, KalshiSeries] = {}
    for r in rows:
        k_ticker = r["kalshi"]["ticker"]
        p_slug = r["polymarket_us"]["ticker"]
        k_market = await fetch_market(config, run, k_ticker, sink=sink)
        k_event = await fetch_event(config, run, k_market.event_ticker, sink=sink)
        series = series_cache.get(k_event.series_ticker)
        if series is None and k_event.series_ticker:
            series = await fetch_series(config, run, k_event.series_ticker, sink=sink)
            series_cache[k_event.series_ticker] = series
        fee_type = k_event.fee_type_override or (series.fee_type if series else "quadratic")
        mult = (
            k_event.fee_multiplier_override
            if k_event.fee_multiplier_override is not None
            else (series.fee_multiplier if series else Decimal(1))
        )
        pm_market = pm_by_slug.get(p_slug)
        coef = (
            pm_market.fee_coefficient
            if pm_market is not None and pm_market.fee_coefficient is not None
            else Decimal("0.06")
        )
        out.kalshi_details.append((k_market, k_event))
        if pm_market is not None:
            out.polymarket_markets[p_slug] = pm_market
        label = f"{r['kalshi'].get('event_title', '')} — {r['kalshi'].get('outcome', '')}"
        out.tracked.append(
            TrackedPair(
                pair_id=int(r["id"]),
                score=float(r["score"]),
                kalshi_market_id=r["kalshi"]["market_id"],
                polymarket_market_id=r["polymarket_us"]["market_id"],
                kalshi_fee=partial(
                    kalshi_fee_ticks,
                    taker=True,
                    fee_type=fee_type,
                    fee_multiplier=Fraction(mult),
                ),
                polymarket_fee=partial(polymarket_fee_ticks, taker=True, fee_coefficient=coef),
                label=label,
                kalshi_ticker=k_ticker,
                polymarket_ticker=p_slug,
                fee_info={
                    "kalshi_fee_type": fee_type,
                    "kalshi_fee_multiplier": str(mult),
                    "polymarket_fee_coefficient": str(coef),
                },
            )
        )
        if k_ticker not in out.kalshi_tickers:
            out.kalshi_tickers.append(k_ticker)
        if p_slug not in out.polymarket_slugs:
            out.polymarket_slugs.append(p_slug)
    log.info("tracked pairs: %d confirmed pairs resolved", len(out.tracked))
    return out

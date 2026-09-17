"""Load the WATCHED pairs — ``status='confirmed' AND tracked`` — as
:class:`TrackedPair`s, with both venues' fee parameters resolved from the
documented endpoints (recorded before parsing).

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
from arb.metrics import PAIRS_EXPIRED_SKIPPED
from arb.pairs import store as pairs_store
from arb.run import RunContext
from arb.types import RawMessage
from arb.venues.kalshi.discovery import fetch_event, fetch_market, fetch_series
from arb.venues.kalshi.rest import KalshiEvent, KalshiMarket, KalshiSeries
from arb.venues.polymarket_us.discovery import fetch_markets_by_slug
from arb.venues.polymarket_us.rest import PolymarketUSMarket

log = logging.getLogger(__name__)


def _dead_reason(market: KalshiMarket, pm: PolymarketUSMarket | None) -> str | None:
    """Why this pair cannot be watched right now, or None to watch it.

    The stored ``close_time`` a selection filters on is a snapshot from
    proposal time, and it is not the whole truth: a Kalshi market that can
    close early closes before its published time, and Polymarket rewrites its
    end date at settlement. This is the second check, and it costs nothing —
    both objects are already fetched here to resolve fees.

    Both predicates are the ones the venue adapters already use, not new ones:
    Kalshi's object-level status enum is tested positively (``== "active"``, as
    in ``kalshi/discovery.py``) so an unlisted future value fails closed, and
    Polymarket is gated on ``closed``, the same half of the liveness test in
    ``polymarket_us/discovery.py``. ``active`` is deliberately not used on the
    Polymarket side: it stays true after settlement.

    A missing Polymarket market means UNKNOWN, not dead. A gateway hiccup must
    never silently empty the watch set.
    """
    if market.status != "active":
        return f"kalshi {market.status}"
    if pm is not None and pm.closed:
        return "polymarket closed"
    return None


@dataclass
class TrackedLoad:
    tracked: list[TrackedPair] = field(default_factory=list)
    kalshi_tickers: list[str] = field(default_factory=list)
    polymarket_slugs: list[str] = field(default_factory=list)
    kalshi_details: list[tuple[KalshiMarket, KalshiEvent]] = field(default_factory=list)
    polymarket_markets: dict[str, PolymarketUSMarket] = field(default_factory=dict)
    # (pair_id, why) for pairs the venues say are over. Defaulted, because
    # tests construct TrackedLoad() with no arguments.
    expired: list[tuple[int, str]] = field(default_factory=list)


async def load_tracked_pairs(
    config: AppConfig,
    run: RunContext,
    engine: AsyncEngine,
    *,
    sink: Callable[[RawMessage], object] | None = None,
    top_n: int | None = None,
) -> TrackedLoad:
    """Every ``confirmed`` pair whose ``tracked`` flag is set, fee parameters
    resolved. Ordered by score for display stability, NOT sliced by it.

    The slice is gone on purpose. It used to be "top N confirmed by score",
    which with tied scores is ``ORDER BY score DESC, id`` — so the lowest ids
    won forever and a pair confirmed today could never be watched, however
    many times the operator reloaded. What to watch is now state
    (``pairs.tracked``), set deliberately because each tracked pair costs one
    Polymarket poll target on a fixed global budget.

    ``top_n`` is accepted and IGNORED for one milestone so the two callers
    outside this change (``arb.ui.server`` and ``arb.replay``) keep type
    checking; it warns when passed and must be deleted from both.
    """
    if top_n is not None:
        log.warning(
            "load_tracked_pairs: top_n=%s ignored — the tracked flag selects the watch set",
            top_n,
        )
    out = TrackedLoad()
    rows = await pairs_store.list_pairs(engine, status="confirmed", tracked=True)
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
        dead = _dead_reason(k_market, pm_market)
        if dead is not None:
            # Skipped, NOT untracked. Clearing the flag here would make loading
            # data mutate operator state from a read path; if the operator
            # wants it cleared, that is a control action with an audit row.
            out.expired.append((int(r["id"]), dead))
            PAIRS_EXPIRED_SKIPPED.labels(venue=dead.split()[0]).inc()
            log.info("tracked pairs: pair %s skipped — %s", r["id"], dead)
            continue
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
    log.info(
        "tracked pairs: %d resolved, %d skipped as settled", len(out.tracked), len(out.expired)
    )
    return out

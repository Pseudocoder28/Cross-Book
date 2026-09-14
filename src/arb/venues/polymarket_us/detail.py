"""DES payload for a Polymarket US market — same shape as the Kalshi one so
the terminal renders both without branching."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from arb.types import ticks_from_dollars
from arb.venues.polymarket_us.rest import (
    Amount,
    PolymarketUSBookStats,
    PolymarketUSEvent,
    PolymarketUSMarket,
    market_id,
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _amount_ticks(amount: Amount | None) -> int | None:
    if amount is None or not amount.value:
        return None
    try:
        return ticks_from_dollars(amount.value)
    except ValueError:
        return None


def build_market_detail(
    market: PolymarketUSMarket,
    event: PolymarketUSEvent | None,
    stats: PolymarketUSBookStats | None,
    *,
    source: str,
    fetched_at_ms: int,
) -> dict[str, Any]:
    status = market.status.removeprefix("MARKET_STATUS_").lower() if market.status else ""
    tick = market.order_price_min_tick_size
    return {
        "market_id": market_id(market.slug),
        "venue": "polymarket_us",
        "ticker": market.slug,
        "event_ticker": event.ticker or event.slug if event else "",
        "series_ticker": event.series_slug if event else "",
        "event_title": market.question or (event.title if event else ""),
        "event_sub_title": event.title if event and event.title != market.question else "",
        "category": market.category or (event.category if event else ""),
        "mutually_exclusive": None,
        "settlement_sources": [],
        "yes_sub_title": market.title,
        "no_sub_title": "",
        "market_type": "binary",
        "status": status,
        "result": "",
        "can_close_early": None,
        "open_time": _iso(market.start_date),
        "close_time": _iso(market.end_date),
        "expected_expiration_time": _iso(market.end_date),
        "rules_primary": market.description,
        "rules_secondary": "",
        "volume": (stats.shares_traded / 10_000) if stats else 0.0,
        "volume_24h": 0.0,
        "open_interest": (stats.open_interest / 10_000) if stats else 0.0,
        "last_price_ticks": stats.last_trade_ticks if stats else None,
        "yes_bid_ticks": _amount_ticks(market.best_bid_quote),
        "yes_ask_ticks": _amount_ticks(market.best_ask_quote),
        # Venue-specific extras the DES page shows when present.
        "tick_size_ticks": int(tick * 10_000) if tick is not None else None,
        "fee_coefficient": str(market.fee_coefficient)
        if market.fee_coefficient is not None
        else None,
        "min_trade_qty": market.minimum_trade_qty,
        "source": source,
        "fetched_at_ms": fetched_at_ms,
    }

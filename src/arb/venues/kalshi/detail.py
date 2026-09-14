"""Market description ("DES") payload for the terminal UI.

Maps Kalshi market + event metadata into the venue-agnostic detail shape the
UI renders. Prices leave here as integer ticks; volumes and open interest as
floats (analytics only, per project rules).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from arb.types import ticks_from_dollars
from arb.venues.kalshi.rest import KalshiEvent, KalshiMarket, market_id


def _ticks(text: str) -> int | None:
    try:
        return ticks_from_dollars(text) if text else None
    except ValueError:
        return None


def _num(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return 0.0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def build_market_detail(
    market: KalshiMarket,
    event: KalshiEvent | None,
    *,
    source: str,
    fetched_at_ms: int,
) -> dict[str, Any]:
    """``source`` is "live" (just fetched) or "discovery" (startup snapshot)."""
    return {
        "market_id": market_id(market.ticker),
        "venue": "kalshi",
        "ticker": market.ticker,
        "event_ticker": market.event_ticker,
        "series_ticker": event.series_ticker if event else "",
        "event_title": event.title if event else "",
        "event_sub_title": event.sub_title if event else "",
        "category": event.category if event else "",
        "mutually_exclusive": event.mutually_exclusive if event else None,
        "settlement_sources": (
            [{"name": s.name, "url": s.url} for s in event.settlement_sources] if event else []
        ),
        "yes_sub_title": market.yes_sub_title,
        "no_sub_title": market.no_sub_title,
        "market_type": market.market_type,
        "status": market.status,
        "result": market.result,
        "can_close_early": market.can_close_early,
        "open_time": _iso(market.open_time),
        "close_time": _iso(market.close_time),
        "expected_expiration_time": _iso(market.expected_expiration_time),
        "rules_primary": market.rules_primary,
        "rules_secondary": market.rules_secondary,
        "volume": _num(market.volume_fp),
        "volume_24h": _num(market.volume_24h_fp),
        "open_interest": _num(market.open_interest_fp),
        "last_price_ticks": _ticks(market.last_price_dollars),
        "yes_bid_ticks": _ticks(market.yes_bid_dollars),
        "yes_ask_ticks": _ticks(market.yes_ask_dollars),
        "source": source,
        "fetched_at_ms": fetched_at_ms,
    }

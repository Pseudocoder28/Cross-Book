"""Polymarket US MarketDataAdapter over REST book polls.

Each poll is a full book, so every frame becomes one unsequenced
:class:`BookSnapshot` (no sequence numbers exist on this venue —
venue-notes). The book's activity stats are kept per market for the DES page
and the monitor; ``last_transact_ts_ms`` lets the caller measure how old a
snapshot already was when it arrived.
"""

from __future__ import annotations

from arb.interfaces import BookEvent, ParseError
from arb.types import RawMessage
from arb.venues.polymarket_us.rest import (
    PolymarketUSBookStats,
    market_id,
    parse_book_response,
    parse_book_stats,
)

VENUE = "polymarket_us"


class PolymarketUSMarketDataAdapter:
    def __init__(self) -> None:
        self.stats: dict[str, PolymarketUSBookStats] = {}  # market_id -> latest stats
        self.last_transact_ts_ms: int | None = None

    @property
    def venue(self) -> str:
        return VENUE

    def parse(self, raw: RawMessage) -> list[BookEvent]:
        if raw.stream != "rest:book":
            return []
        snapshot = parse_book_response(raw)
        try:
            stats = parse_book_stats(raw)
        except ParseError:
            stats = None
        if stats is not None:
            self.stats[market_id(stats.slug)] = stats
            self.last_transact_ts_ms = (
                int(stats.transact_time.timestamp() * 1000) if stats.transact_time else None
            )
        return [snapshot]

"""Kalshi REST response parsers.

Shapes verified against the docs (docs/venue-notes.md, 2026-09-13) and the
real captured payloads in ``tests/fixtures/kalshi/``. Read-only: parsers only.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from arb.book import BookSnapshot, Level
from arb.interfaces import ParseError
from arb.types import RawMessage, complement, qty_from_contracts, ticks_from_dollars

VENUE = "kalshi"


def market_id(ticker: str) -> str:
    """Normalized cross-venue market id: venue-qualified native identifier."""
    return f"{VENUE}:{ticker}"


class KalshiMarket(BaseModel):
    """The slice of a Kalshi market object the system uses; extras ignored.

    Field names per https://docs.kalshi.com/api-reference/market/get-markets.md
    (`title`/`subtitle` are deprecated there; rules and sub-titles carry the
    matching signal for the pair matcher).
    """

    model_config = ConfigDict(extra="ignore")

    ticker: str
    event_ticker: str
    market_type: str
    status: str
    result: str = ""
    open_time: datetime | None = None
    close_time: datetime | None = None
    expected_expiration_time: datetime | None = None
    rules_primary: str = ""
    rules_secondary: str = ""
    yes_sub_title: str = ""
    no_sub_title: str = ""
    # Fixed-point strings straight from the venue; converted at the edge.
    volume_fp: str = "0"
    volume_24h_fp: str = "0"
    open_interest_fp: str = "0"
    last_price_dollars: str = ""
    yes_bid_dollars: str = ""
    yes_ask_dollars: str = ""
    can_close_early: bool | None = None


class KalshiSettlementSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    url: str = ""


class KalshiEvent(BaseModel):
    """Event metadata per
    https://docs.kalshi.com/api-reference/events/get-event.md (``category`` is
    deprecated there but still populated in live payloads)."""

    model_config = ConfigDict(extra="ignore")

    event_ticker: str
    series_ticker: str = ""
    title: str = ""
    sub_title: str = ""
    category: str = ""
    mutually_exclusive: bool | None = None
    settlement_sources: list[KalshiSettlementSource] = []


def parse_markets_response(raw: RawMessage) -> tuple[list[KalshiMarket], str]:
    """``GET /markets`` → (markets, next cursor). Empty cursor means done."""
    try:
        doc = json.loads(raw.payload)
        markets = [KalshiMarket.model_validate(m) for m in doc["markets"]]
        cursor = doc.get("cursor") or ""
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"kalshi markets response: {exc}") from exc
    return markets, cursor


def parse_market_response(raw: RawMessage) -> KalshiMarket:
    """``GET /markets/{ticker}`` → one market
    (https://docs.kalshi.com/api-reference/market/get-market.md)."""
    try:
        return KalshiMarket.model_validate(json.loads(raw.payload)["market"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"kalshi market response: {exc}") from exc


def parse_event_response(raw: RawMessage) -> KalshiEvent:
    """``GET /events/{event_ticker}`` → event metadata
    (https://docs.kalshi.com/api-reference/events/get-event.md)."""
    try:
        return KalshiEvent.model_validate(json.loads(raw.payload)["event"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"kalshi event response: {exc}") from exc


def parse_orderbook_response(raw: RawMessage, *, ticker: str) -> BookSnapshot:
    """``GET /markets/{ticker}/orderbook`` → normalized YES-book snapshot.

    Kalshi publishes bids only: ``yes_dollars`` are YES bids and
    ``no_dollars`` are NO bids, i.e. YES asks at the complement price. The
    response carries no ticker or sequence number, so the caller supplies the
    ticker and ``seq`` is None.
    """
    try:
        doc = json.loads(raw.payload)
        book = doc["orderbook_fp"]
        bids = tuple(
            Level(ticks_from_dollars(px), qty_from_contracts(count))
            for px, count in book.get("yes_dollars") or []
        )
        asks = tuple(
            Level(complement(ticks_from_dollars(px)), qty_from_contracts(count))
            for px, count in book.get("no_dollars") or []
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"kalshi orderbook response: {exc}") from exc
    return BookSnapshot(market_id=market_id(ticker), bids=bids, asks=asks, seq=None)

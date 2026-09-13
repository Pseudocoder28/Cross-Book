"""Polymarket US gateway REST response parsers.

Shapes verified against docs.polymarket.us (docs/venue-notes.md, 2026-09-13)
and the real captured payloads in ``tests/fixtures/polymarket_us/``. Only the
public gateway (no auth) is touched here. Read-only: parsers only.

JSON numbers are parsed as ``Decimal`` so per-market tick size and fee
coefficient stay exact — no floats for prices or fees.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arb.book import BookSnapshot, Level
from arb.interfaces import ParseError
from arb.types import RawMessage, qty_from_contracts, ticks_from_dollars

VENUE = "polymarket_us"


def market_id(slug: str) -> str:
    """Normalized cross-venue market id: venue-qualified native identifier."""
    return f"{VENUE}:{slug}"


class PolymarketUSMarket(BaseModel):
    """The slice of a gateway market object the system uses; extras ignored.

    Field names per
    https://docs.polymarket.us/api-reference/markets/get-markets — one
    instrument per market (YES); ``slug`` is the identifier used everywhere.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    slug: str
    question: str = ""
    title: str = ""
    description: str = ""
    category: str = ""
    active: bool
    closed: bool
    archived: bool = False
    hidden: bool = False
    status: str = ""
    start_date: datetime | None = Field(default=None, alias="startDate")
    end_date: datetime | None = Field(default=None, alias="endDate")
    game_start_time: datetime | None = Field(default=None, alias="gameStartTime")
    minimum_trade_qty: int | None = Field(default=None, alias="minimumTradeQty")
    order_price_min_tick_size: Decimal | None = Field(default=None, alias="orderPriceMinTickSize")
    fee_coefficient: Decimal | None = Field(default=None, alias="feeCoefficient")


def _loads(payload: bytes) -> Any:
    return json.loads(payload, parse_float=Decimal)


def parse_markets_response(raw: RawMessage) -> list[PolymarketUSMarket]:
    """``GET /v1/markets`` → market objects. Pagination is limit/offset."""
    try:
        doc = _loads(raw.payload)
        return [PolymarketUSMarket.model_validate(m) for m in doc["markets"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"polymarket_us markets response: {exc}") from exc


def _level(entry: Any) -> Level:
    px = entry["px"]
    currency = px.get("currency")
    if currency != "USD":
        raise ValueError(f"unexpected currency: {currency!r}")
    return Level(ticks_from_dollars(px["value"]), qty_from_contracts(entry["qty"]))


def parse_book_response(raw: RawMessage) -> BookSnapshot:
    """``GET /v1/markets/{slug}/book`` → normalized YES-book snapshot.

    One instrument per market: ``bids``/``offers`` are already the YES book.
    No sequence numbers exist on this venue, so ``seq`` is None and validity
    rests on staleness plus periodic re-snapshots.
    """
    try:
        doc = _loads(raw.payload)
        market_data = doc["marketData"]
        slug = market_data["marketSlug"]
        bids = tuple(_level(entry) for entry in market_data.get("bids") or [])
        asks = tuple(_level(entry) for entry in market_data.get("offers") or [])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"polymarket_us book response: {exc}") from exc
    return BookSnapshot(market_id=market_id(slug), bids=bids, asks=asks, seq=None)

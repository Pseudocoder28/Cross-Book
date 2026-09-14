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


WEB_BASE = "https://polymarket.us"


def event_url(event_slug: str) -> str | None:
    """Human-facing page for an event, or None without an event slug.

    Verified live 2026-09-14: ``/event/<event-slug>`` serves the market page
    (200, title matches), while the *market* slug does not route there and is
    not what the site's search box matches — which is why pasting a market
    slug into the site finds nothing. See docs/venue-notes.md.
    """
    return f"{WEB_BASE}/event/{event_slug}" if event_slug else None


class Amount(BaseModel):
    """Gateway money object: a decimal dollar string plus currency."""

    model_config = ConfigDict(extra="ignore")

    value: str = ""
    currency: str = "USD"


class PolymarketUSMarket(BaseModel):
    """The slice of a gateway market object the system uses; extras ignored.

    Field names per
    https://docs.polymarket.us/api-reference/markets/get-markets — one
    instrument per market (YES); ``slug`` is the identifier used everywhere.
    Live listings carry no volume fields (docs list them; reality omits them —
    venue-notes), so activity comes from the book's ``stats`` instead.
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
    # Docs say whole contracts only; live listings carry 0.01 for some
    # markets (venue-notes), so this is a Decimal, not an int.
    minimum_trade_qty: Decimal | None = Field(default=None, alias="minimumTradeQty")
    order_price_min_tick_size: Decimal | None = Field(default=None, alias="orderPriceMinTickSize")
    fee_coefficient: Decimal | None = Field(default=None, alias="feeCoefficient")
    best_bid_quote: Amount | None = Field(default=None, alias="bestBidQuote")
    best_ask_quote: Amount | None = Field(default=None, alias="bestAskQuote")


class PolymarketUSEvent(BaseModel):
    """Event metadata per
    https://docs.polymarket.us/api-reference/events/get-events (extras ignored).
    Nested ``markets`` are parsed separately."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = ""
    slug: str
    ticker: str = ""
    title: str = ""
    description: str = ""
    category: str = ""
    series_slug: str = Field(default="", alias="seriesSlug")
    active: bool = True
    closed: bool = False
    start_date: datetime | None = Field(default=None, alias="startDate")
    end_date: datetime | None = Field(default=None, alias="endDate")


class PolymarketUSBookStats(BaseModel):
    """Activity numbers that ride along with ``GET /v1/markets/{slug}/book``."""

    model_config = ConfigDict(extra="ignore")

    slug: str
    state: str = ""
    transact_time: datetime | None = None
    shares_traded: int = 0  # Qty units (0.0001 contracts)
    open_interest: int = 0  # Qty units
    last_trade_ticks: int | None = None


def _loads(payload: bytes) -> Any:
    return json.loads(payload, parse_float=Decimal)


def parse_markets_response(raw: RawMessage) -> list[PolymarketUSMarket]:
    """``GET /v1/markets`` → market objects. Pagination is limit/offset."""
    try:
        doc = _loads(raw.payload)
        return [PolymarketUSMarket.model_validate(m) for m in doc["markets"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"polymarket_us markets response: {exc}") from exc


def parse_events_response(
    raw: RawMessage,
) -> list[tuple[PolymarketUSEvent, list[PolymarketUSMarket]]]:
    """``GET /v1/events`` → (event, nested markets) pairs. Markets that fail
    validation are skipped, never fatal."""
    try:
        doc = _loads(raw.payload)
        out: list[tuple[PolymarketUSEvent, list[PolymarketUSMarket]]] = []
        for event_doc in doc["events"]:
            event = PolymarketUSEvent.model_validate(event_doc)
            markets: list[PolymarketUSMarket] = []
            for market_doc in event_doc.get("markets") or []:
                try:
                    markets.append(PolymarketUSMarket.model_validate(market_doc))
                except ValueError:
                    continue
            out.append((event, markets))
        return out
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"polymarket_us events response: {exc}") from exc


def _amount_ticks(obj: Any) -> int | None:
    if not isinstance(obj, dict):
        return None
    value = obj.get("value")
    if not isinstance(value, str) or not value:
        return None
    try:
        return ticks_from_dollars(value)
    except ValueError:
        return None


def parse_book_stats(raw: RawMessage) -> PolymarketUSBookStats:
    """Activity stats from ``GET /v1/markets/{slug}/book`` (same payload as
    :func:`parse_book_response`; fields per venue-notes and the captured
    fixture)."""
    try:
        market_data = _loads(raw.payload)["marketData"]
        stats = market_data.get("stats") or {}
        transact = market_data.get("transactTime")
        return PolymarketUSBookStats(
            slug=market_data["marketSlug"],
            state=str(market_data.get("state") or ""),
            transact_time=datetime.fromisoformat(_clip_ns(transact)) if transact else None,
            shares_traded=qty_from_contracts(str(stats.get("sharesTraded") or "0")),
            open_interest=qty_from_contracts(str(stats.get("openInterest") or "0")),
            last_trade_ticks=_amount_ticks(stats.get("lastTradePx")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"polymarket_us book stats: {exc}") from exc


def _clip_ns(ts: str) -> str:
    """RFC3339 with nanoseconds → microseconds, which fromisoformat accepts."""
    if "." in ts:
        head, tail = ts.split(".", 1)
        digits = "".join(ch for ch in tail if ch.isdigit())
        suffix = tail[len(digits) :]
        return f"{head}.{digits[:6]}{suffix}"
    return ts


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

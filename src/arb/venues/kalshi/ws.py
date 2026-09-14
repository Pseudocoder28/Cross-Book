"""Kalshi WebSocket message parsing and subscribe commands.

Wire format verified against https://docs.kalshi.com/asyncapi.yaml and
https://docs.kalshi.com/getting_started/quick_start_websockets.md (recorded
in docs/venue-notes.md), and tested against real captured frames in
``tests/fixtures/kalshi/``.

Envelope: ``{"type": ..., "sid": ..., "seq": ..., "msg": {...}}``.
``orderbook_snapshot`` msg carries ``market_ticker``, ``yes_dollars_fp`` and
``no_dollars_fp`` (arrays of ``[price_dollars, count_fp]``);
``orderbook_delta`` msg carries ``market_ticker``, ``price_dollars``,
``delta_fp`` (signed) and ``side`` (``"yes"``/``"no"``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from arb.book import BookLevelUpdate, BookSnapshot, Level, UpdateMode
from arb.interfaces import ParseError
from arb.types import (
    BookSide,
    RawMessage,
    complement,
    qty_delta_from_contracts,
    qty_from_contracts,
    ticks_from_dollars,
)
from arb.venues.kalshi.rest import market_id

ORDERBOOK_CHANNEL = "orderbook_delta"


def subscribe_orderbook_cmd(cmd_id: int, market_tickers: Sequence[str]) -> bytes:
    """Subscribe command per the WS quickstart doc."""
    return json.dumps(
        {
            "id": cmd_id,
            "cmd": "subscribe",
            "params": {"channels": [ORDERBOOK_CHANNEL], "market_tickers": list(market_tickers)},
        }
    ).encode()


def parse_ws_message(raw: RawMessage) -> list[BookSnapshot | BookLevelUpdate]:
    """One WS frame → zero or more normalized book events.

    Non-book frames (``subscribed``, errors, other channels) yield ``[]`` —
    the recorder archived the raw frame either way. Malformed frames raise
    :class:`ParseError`.
    """
    try:
        doc = json.loads(raw.payload)
    except ValueError as exc:
        raise ParseError(f"kalshi ws message: {exc}") from exc
    return book_events_from_doc(doc)


def book_events_from_doc(doc: Any) -> list[BookSnapshot | BookLevelUpdate]:
    """Normalized book events from one already-decoded WS envelope.

    Shared by :func:`parse_ws_message` and the adapter (which json-loads once
    and also tracks the envelope ``seq`` per ``sid``).
    """
    try:
        msg_type = doc.get("type")
        if msg_type == "orderbook_snapshot":
            msg = doc["msg"]
            bids = tuple(
                Level(ticks_from_dollars(px), qty_from_contracts(count))
                for px, count in msg.get("yes_dollars_fp") or []
            )
            asks = tuple(
                Level(complement(ticks_from_dollars(px)), qty_from_contracts(count))
                for px, count in msg.get("no_dollars_fp") or []
            )
            return [
                BookSnapshot(
                    market_id=market_id(msg["market_ticker"]),
                    bids=bids,
                    asks=asks,
                    seq=doc.get("seq"),
                )
            ]
        if msg_type == "orderbook_delta":
            msg = doc["msg"]
            price = ticks_from_dollars(msg["price_dollars"])
            delta = qty_delta_from_contracts(msg["delta_fp"])
            side = msg["side"]
            if side == "yes":
                book_side, level_price = BookSide.BID, price
            elif side == "no":
                # A NO-bid change is a YES-ask change at the complement price.
                book_side, level_price = BookSide.ASK, complement(price)
            else:
                raise ValueError(f"unknown side: {side!r}")
            return [
                BookLevelUpdate(
                    market_id=market_id(msg["market_ticker"]),
                    side=book_side,
                    price=level_price,
                    qty=delta,
                    mode=UpdateMode.DELTA,
                    seq=doc.get("seq"),
                )
            ]
        return []
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"kalshi ws message: {exc}") from exc
